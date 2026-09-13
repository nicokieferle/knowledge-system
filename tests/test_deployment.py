from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_review_service_is_private_and_separate():
    compose = (PROJECT_ROOT / "compose.server.yml").read_text(encoding="utf-8")
    review = compose.split("  review-web:", 1)[1]
    assert 'command: ["knowledge-review"]' in review
    assert "host_ip: ${REVIEW_BIND_ADDRESS:-127.0.0.1}" in review
    assert "REVIEW_MODE: production" in review
    assert "REVIEW_PASSWORD_HASH: ${REVIEW_PASSWORD_HASH:?" in review
    assert "REVIEW_SESSION_SECRET: ${REVIEW_SESSION_SECRET:?" in review
    assert "read_only: true" in review and "cap_drop: [ALL]" in review
    assert "docker.sock" not in review and "privileged:" not in review
    assert "TELEGRAM_BOT_TOKEN" not in review and "LLM_API_KEY" not in review


def test_server_compose_preserves_local_only_and_persistent_boundaries() -> None:
    compose = (PROJECT_ROOT / "compose.server.yml").read_text(encoding="utf-8")

    assert "host_ip: 127.0.0.1" in compose
    assert "MCP_HOST: 0.0.0.0" in compose
    assert "MCP_ALLOWED_HOSTS:" in compose
    assert "condition: service_healthy" in compose
    assert "source: ./knowledge" in compose
    assert "target: /app/knowledge" in compose
    assert "read_only: true" in compose
    assert "/data/knowledgesystem/postgres:/var/lib/postgresql/data" in compose
    assert "source: /data/knowledgesystem/huggingface" in compose
    assert "target: /home/knowledge/.cache/huggingface" in compose
    assert "POSTGRES_PASSWORD: knowledge" not in compose
    assert "8000:8000" not in compose


def test_server_image_is_non_root_cpu_only_and_does_not_copy_environment() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.12-slim-bookworm")
    assert "https://download.pytorch.org/whl/cpu" in dockerfile
    assert "USER knowledge" in dockerfile
    assert "chmod -R a+rX /app/src /app/scripts" in dockerfile
    assert 'CMD ["knowledge-mcp"]' in dockerfile
    assert "COPY . ." not in dockerfile
    assert "COPY .env" not in dockerfile
    assert ".env" in dockerignore
    assert "knowledge" in dockerignore


def test_debian_documentation_has_durable_backup_and_defensive_restore_commands() -> None:
    deployment = (PROJECT_ROOT / "docs" / "DEPLOYMENT_DEBIAN.md").read_text(encoding="utf-8")

    assert "bash scripts/backup_durable_state.sh" in deployment
    assert "pg_restore --list" in deployment
    assert "bash scripts/restore_durable_state.sh" in deployment
    assert "durable_state_fingerprint.py" in deployment
    assert "does not restore directly into the configured production database" in deployment
    assert "and any non-empty target" in deployment
    assert "client_states" in deployment
    assert "client_conversations" in deployment
    assert "client_message_bindings" in deployment
    assert "Do not use `docker compose ... down -v`" in deployment
    assert "/data/knowledgesystem/backups" in deployment
