from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"


def test_backup_uses_restrictive_custom_format_and_shared_table_allowlist() -> None:
    common = (SCRIPTS / "durable_tables.sh").read_text(encoding="utf-8")
    backup = (SCRIPTS / "backup_durable_state.sh").read_text(encoding="utf-8")

    for table in (
        "conversations",
        "messages",
        "conversation_summaries",
        "proposal_suggestions",
        "proposals",
    ):
        assert common.count(f"  {table}\n") == 1
    assert "source \"${SCRIPT_DIR}/durable_tables.sh\"" in backup
    assert "--format=custom" in backup
    assert "--data-only" in backup
    assert "umask 077" in backup
    assert "chmod 0600" in backup
    assert "/data/knowledgesystem/backups" in backup
    assert '== "/data/knowledgesystem/postgres"*' in backup
    assert "POSTGRES_PASSWORD" not in backup


def test_restore_is_defensive_and_never_cleans_existing_database() -> None:
    restore = (SCRIPTS / "restore_durable_state.sh").read_text(encoding="utf-8")

    assert "source \"${SCRIPT_DIR}/durable_tables.sh\"" in restore
    assert "Refusing to restore directly" in restore
    assert "Refusing to restore into a database containing durable rows" in restore
    assert restore.index("existing_count=") < restore.index("pg_restore --username")
    assert "--single-transaction" in restore
    assert "--exit-on-error" in restore
    assert "--clean" not in restore
    assert "DROP " not in restore
    assert "TRUNCATE " not in restore


def test_smoke_compose_is_isolated_from_production_resources() -> None:
    compose = (PROJECT_ROOT / "compose.v30-smoke.yml").read_text(encoding="utf-8")
    runner = (SCRIPTS / "run_v30_postgres_recovery_smoke.sh").read_text(encoding="utf-8")

    assert "pgvector/pgvector:0.8.6-pg17-bookworm" in compose
    assert "ports:" not in compose
    assert "/data/knowledgesystem/postgres" not in compose
    assert "postgres_data:/var/lib/postgresql/data" in compose
    assert "read_only: true" in compose
    assert "knowledge-v30-smoke-" in runner
    assert "down --volumes --remove-orphans --rmi local" in runner
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
