from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from knowledge_system.durable_state import DURABLE_TABLES
from knowledge_system.review_schema import REVIEW_SCHEMA

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"


def _bash() -> str | None:
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash")


def test_backup_uses_restrictive_custom_format_and_shared_table_allowlist() -> None:
    common = (SCRIPTS / "durable_tables.sh").read_text(encoding="utf-8")
    backup = (SCRIPTS / "backup_durable_state.sh").read_text(encoding="utf-8")

    for table in (
        "conversations",
        "client_states",
        "client_conversations",
        "client_message_bindings",
        "messages",
        "conversation_summaries",
        "proposal_suggestions",
        "proposals",
    ):
        assert common.count(f"  {table}\n") == 1
    assert 'source "${SCRIPT_DIR}/durable_tables.sh"' in backup
    assert "--format=custom" in backup
    assert "--data-only" in backup
    assert "pg_restore --list" in backup
    assert "backup_archive_check=pass" in backup
    assert "umask 077" in backup
    assert "chmod 0600" in backup
    assert "/data/knowledgesystem/backups" in backup
    assert '== "/data/knowledgesystem/postgres"*' in backup
    assert "POSTGRES_PASSWORD" not in backup


def test_durable_table_allowlist_preserves_foreign_key_restore_order() -> None:
    common = (SCRIPTS / "durable_tables.sh").read_text(encoding="utf-8")
    start = common.index("readonly DURABLE_TABLES=(")
    end = common.index(")", start)
    tables = [line.strip() for line in common[start:end].splitlines() if line.startswith("  ")]

    assert tables == [
        "conversations",
        "client_states",
        "client_conversations",
        "client_message_bindings",
        "messages",
        "conversation_summaries",
        "proposal_suggestions",
        "proposals",
        "proposal_reviews",
        "proposal_decisions",
    ]
    assert tables == list(DURABLE_TABLES)


def test_dump_and_restore_use_their_distinct_table_filter_syntax() -> None:
    common = (SCRIPTS / "durable_tables.sh").read_text(encoding="utf-8")
    backup = (SCRIPTS / "backup_durable_state.sh").read_text(encoding="utf-8")
    restore = (SCRIPTS / "restore_durable_state.sh").read_text(encoding="utf-8")

    assert 'DURABLE_PG_DUMP_TABLE_ARGS+=("--table=public.${table}")' in common
    assert 'DURABLE_PG_RESTORE_FILTER_ARGS=("--schema=public")' in common
    assert 'DURABLE_PG_RESTORE_FILTER_ARGS+=("--table=${table}")' in common
    assert "durable_integrity_sql" in common
    assert "durable_pg_dump_table_args" in backup
    assert '"${DURABLE_PG_DUMP_TABLE_ARGS[@]}"' in backup
    assert "durable_pg_restore_filter_args" in restore
    assert '"${DURABLE_PG_RESTORE_FILTER_ARGS[@]}"' in restore
    assert 'DURABLE_PG_RESTORE_FILTER_ARGS+=("--table=public.${table}")' not in common


def test_restore_is_defensive_and_never_cleans_existing_database() -> None:
    restore = (SCRIPTS / "restore_durable_state.sh").read_text(encoding="utf-8")
    common = (SCRIPTS / "durable_tables.sh").read_text(encoding="utf-8")

    assert 'source "${SCRIPT_DIR}/durable_tables.sh"' in restore
    assert "Refusing to restore directly" in restore
    assert "Refusing to restore into a database containing durable rows" in restore
    assert restore.index("existing_count=") < restore.index("pg_restore --username")
    assert "--single-transaction" in restore
    assert "--exit-on-error" in restore
    assert "--disable-triggers" not in restore
    assert "--set=ON_ERROR_STOP=1" in restore
    assert '--file="$restore_sql" --command="$sequence_sql"' in restore
    assert '--command="$integrity_guard_sql"' in restore
    assert "durable_integrity_guard_sql" in restore
    assert "restore_atomic=true" in restore
    assert "restore_triggers_disabled=false" in restore
    assert "mktemp /tmp/knowledge-durable-restore." in restore
    assert "Archive must contain exactly one data entry for every durable table" in restore
    assert "Restore SQL is missing PostgreSQL restriction guards" in restore
    assert "--no-psqlrc" in restore
    assert "restore_psql_restricted=true" in restore
    assert 'grep -F -v " SEQUENCE SET "' in restore
    assert "ALTER SEQUENCE public.messages_id_seq RESTART" in restore
    assert "SELECT setval(" not in restore
    assert "umask 077" in restore
    assert "Restored durable state failed integrity check" in common
    assert "--clean" not in restore
    assert "DROP " not in restore
    assert "TRUNCATE " not in restore


@pytest.mark.parametrize(
    "database",
    (
        "dbname=knowledge",
        "postgresql:///knowledge",
        "knowledge host=elsewhere",
        "knowledge\nother",
    ),
)
def test_restore_rejects_database_connection_aliases_before_docker(database: str) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("bash is required to exercise the restore entry point")

    result = subprocess.run(
        [bash, str(SCRIPTS / "restore_durable_state.sh"), str(PROJECT_ROOT / "README.md")],
        cwd=PROJECT_ROOT,
        env={**os.environ, "RESTORE_DATABASE": database},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "RESTORE_DATABASE must be a simple local database name\n"


def test_only_the_cyclic_accepted_review_fk_is_initially_deferred() -> None:
    assert (
        "ALTER TABLE proposals ALTER CONSTRAINT proposals_accepted_review_fk\n"
        "        DEFERRABLE INITIALLY DEFERRED;"
    ) in REVIEW_SCHEMA
    assert REVIEW_SCHEMA.count("DEFERRABLE INITIALLY DEFERRED") == 1


def test_smoke_compose_is_isolated_from_production_resources() -> None:
    compose = (PROJECT_ROOT / "compose.v30-smoke.yml").read_text(encoding="utf-8")
    runner = (SCRIPTS / "run_v30_postgres_recovery_smoke.sh").read_text(encoding="utf-8")

    assert "pgvector/pgvector:0.8.6-pg17-bookworm" in compose
    assert "ports:" not in compose
    assert "/data/knowledgesystem/postgres" not in compose
    assert "postgres_data:/var/lib/postgresql/data" in compose
    assert "read_only: true" in compose
    assert "knowledge-v30-smoke-" in runner
    assert "down --volumes --remove-orphans" in runner
    assert "== knowledge-system-v30-smoke:*" in runner
    assert 'docker image rm "${SMOKE_IMAGE_NAME}"' in runner
    assert "--rmi local" not in runner
    assert "docker system prune" not in runner
    assert "compose.server.yml" not in runner
    assert "production_compose_touched=false" in runner


def test_real_postgres_smoke_has_explicit_destructive_target_guard() -> None:
    smoke = (SCRIPTS / "conversation_postgres_smoke.py").read_text(encoding="utf-8")

    assert "V30_SMOKE_CONFIRM" in smoke
    assert "isolated-v30-smoke-database" in smoke
    assert '("smoke", "test")' in smoke
    assert "source_suggestion_id" in smoke
    assert "ThreadPoolExecutor" in smoke
    assert "knowledge_unchanged" in smoke
    assert '"conversation_summaries": 3' in smoke
