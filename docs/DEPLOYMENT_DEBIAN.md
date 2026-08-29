# Debian 12 Docker deployment

This deployment keeps `knowledge/` as the canonical source and PostgreSQL as a disposable,
rebuildable retrieval index. It exposes the MCP endpoint only on the Debian host loopback
interface. It does not provide TLS, authentication or public network access.

## Architecture

```text
Debian Git checkout
  knowledge/ (canonical Markdown, read-only bind mount)
        |
        v
knowledge-mcp container (non-root, CPU, Streamable HTTP on 0.0.0.0:8000)
        |
        +--> PostgreSQL + pgvector container
        |      `--> knowledge_pgdata volume
        |
        `--> knowledge_hf_cache volume

Debian host publishes only 127.0.0.1:8000 -> container port 8000
```

`compose.server.yml` is intentionally separate from `docker-compose.yml`. The existing file
remains the small PostgreSQL-only configuration used by local Windows development.

## Prerequisites

- Debian 12 with current security updates
- Docker Engine and the Docker Compose plugin
- Git
- enough disk space for the container image, PostgreSQL index and roughly 2 GB reranker cache
- enough RAM to run PostgreSQL and the CPU reranker concurrently

Verify the installation:

```bash
docker version
docker compose version
git --version
```

## First installation

Clone the repository and create a server-only environment file:

```bash
git clone https://github.com/Hengsto/Knowledge-System.git
cd Knowledge-System
cp .env.server.example .env.server
chmod 600 .env.server
```

Replace the example database password in both `POSTGRES_PASSWORD` and `DATABASE_URL`.
`openssl rand -hex 32` produces a URL-safe value. Do not commit `.env.server`; it is ignored
by Git and excluded from the Docker build context.

Build the CPU image and start PostgreSQL:

```bash
docker compose --env-file .env.server -f compose.server.yml build knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml up -d postgres
docker compose --env-file .env.server -f compose.server.yml ps
```

Initialize and build the disposable retrieval index explicitly:

```bash
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge init-db
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge index
```

These commands are deliberately not part of container startup. Starting the MCP service
does not initialize the database, rebuild the index, modify Markdown or run Git.

Start the MCP service:

```bash
docker compose --env-file .env.server -f compose.server.yml up -d knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml ps
docker compose --env-file .env.server -f compose.server.yml logs --tail=100 knowledge-mcp
```

The endpoint is now available only on the Debian host:

```text
http://127.0.0.1:8000/mcp
```

## Verify binding and MCP

Confirm Docker publishes only the loopback address:

```bash
docker compose --env-file .env.server -f compose.server.yml port knowledge-mcp 8000
ss -lnt | grep ':8000'
```

The first command must report `127.0.0.1:8000`, not `0.0.0.0:8000`. A connection to the
server's public interface on port 8000 must fail. The exact external-interface test depends
on the host firewall and available second machine; do not add a public firewall rule for it.

Run the standard MCP v2 client contained in the built image against the host endpoint:

```bash
docker run --rm --network host knowledge-system-mcp:local \
  python /app/scripts/mcp_http_smoke.py http://127.0.0.1:8000/mcp
```

The smoke test performs `initialize`, requires exactly `search_knowledge` and `get_document`,
runs one fast search, runs two quality searches in the same MCP process, and fetches the
canonical original document for the top quality result. It reports timings and metadata but
does not print document contents.

The first quality request can take substantially longer while
`BAAI/bge-reranker-v2-m3` is downloaded and loaded. The second request reuses the in-process
reranker. `HF_HOME` points at the persistent `knowledge_hf_cache` volume, so later container
processes reuse the downloaded model files even though they load the model into RAM again.

Inspect the cache without printing its contents:

```bash
docker compose --env-file .env.server -f compose.server.yml exec knowledge-mcp \
  sh -lc 'du -sh "$HF_HOME"'
docker volume inspect knowledge-system_knowledge_hf_cache
```

## Restart and persistence

Record the current chunk count and cache size:

```bash
docker compose --env-file .env.server -f compose.server.yml exec postgres \
  sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM chunks"'
docker compose --env-file .env.server -f compose.server.yml exec knowledge-mcp \
  sh -lc 'du -sh "$HF_HOME"'
```

Restart the service and rerun the MCP smoke test:

```bash
docker compose --env-file .env.server -f compose.server.yml restart knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml ps
docker run --rm --network host knowledge-system-mcp:local \
  python /app/scripts/mcp_http_smoke.py http://127.0.0.1:8000/mcp
```

The PostgreSQL chunk count must be unchanged, `knowledge/` remains available through its
host bind mount, and the Hugging Face cache size remains populated. The new MCP process loads
the reranker into RAM again from cached files.

Verify persistence across a normal Compose shutdown:

```bash
docker compose --env-file .env.server -f compose.server.yml down
docker compose --env-file .env.server -f compose.server.yml up -d
docker compose --env-file .env.server -f compose.server.yml ps
```

Named volumes survive `down`. `docker compose ... down -v` intentionally deletes both the
rebuildable PostgreSQL index and the model cache. Canonical Markdown remains in the Git
checkout and is not affected.

## Logs and stop

```bash
docker compose --env-file .env.server -f compose.server.yml logs -f knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml stop
```

Application logs contain lifecycle and error diagnostics. The deployment does not add query
or full-document logging.

## Updating

For knowledge-only changes, the read-only bind mount makes the new Markdown visible without
rebuilding the image. Pull and reindex explicitly:

```bash
git pull --ff-only
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge index
```

When application code or dependencies changed, rebuild and recreate the service before
reindexing:

```bash
git pull --ff-only
docker compose --env-file .env.server -f compose.server.yml build knowledge-mcp
docker compose --env-file .env.server -f compose.server.yml up -d
docker compose --env-file .env.server -f compose.server.yml run --rm knowledge-mcp knowledge index
docker compose --env-file .env.server -f compose.server.yml ps
```

No Git synchronization, database initialization or indexing runs automatically.
