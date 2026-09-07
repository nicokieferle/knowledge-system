# Knowledge System

Local-first prototype for a long-term personal knowledge base.

## Core rule

**Original knowledge is the source of truth. Retrieval tables are a disposable index.**

For V0:

- canonical knowledge lives as Markdown under `knowledge/`
- Git versions every change
- PostgreSQL + pgvector stores derived chunks and embeddings
- embeddings are generated locally
- only changed chunks are re-embedded
- search uses exact cosine similarity
- no AI is allowed to write directly to `main`

Later versions can add proposals, approval/rejection and other source adapters.

## Architecture

```text
CLI --------------------------+
                              |
MCP Client -- stdio ----------+
MCP Client -- HTTP /mcp ------+
                              v
                         MCP Server
                              |
                              v
                     KnowledgeService
                              |
                              v
                  Retrieval + SourceAdapter
                              |
                              v
GitMarkdownSource -> knowledge/*.md (source of truth)
                              |
                              v
PostgreSQL chunks + vector/full-text indexes (disposable retrieval cache)
```

Indexing still derives everything from source documents:

```text
GitMarkdownSource
        |
        v
Markdown chunker
        |
        v
local embedding model
        |
        v
PostgreSQL + pgvector
```

## Quick start

1. Copy the environment file:

```bash
cp .env.example .env
```

2. Start PostgreSQL:

```bash
docker compose up -d
```

3. Create a virtual environment and install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

4. Initialize the database:

```bash
knowledge init-db
```

5. Index the sample knowledge:

```bash
knowledge index
```

6. Search:

```bash
knowledge search "Wie können Staatsschulden die Geldpolitik beeinflussen?"
```

7. Run the retrieval baseline evaluation:

```bash
knowledge eval --suite eval/retrieval_v01.jsonl
```

The default eval retriever is the semantic vector baseline. A separate PostgreSQL full-text
keyword baseline can be measured without changing the eval suite:

```bash
knowledge eval --suite eval/retrieval_v01.jsonl --retriever keyword --text-config german
```

Experimental Reciprocal Rank Fusion of the vector baseline and German keyword baseline:

```bash
knowledge eval --suite eval/retrieval_v01.jsonl --retriever hybrid
```

Experimental keyword-candidate reranking with `BAAI/bge-reranker-v2-m3`:

```bash
knowledge eval --suite eval/retrieval_v01.jsonl --retriever reranker
```

Product-facing search goes through the internal service boundary:

```bash
knowledge search "Wie können Staatsschulden die Geldpolitik beeinflussen?" --mode quality
knowledge search "Wie können Staatsschulden die Geldpolitik beeinflussen?" --mode fast
```

`quality` uses keyword candidates plus the local reranker. `fast` uses PostgreSQL keyword
search with the German text-search configuration.

## Read-only MCP server

V2 exposes the existing `KnowledgeService` through an official MCP Python SDK v2 server.
The local stdio entry point is intended to be started by an MCP client:

```bash
knowledge-mcp
```

`stdio` remains the default transport. To start the same server and tools locally over
Streamable HTTP in Windows PowerShell:

```powershell
$env:MCP_TRANSPORT="streamable-http"
$env:MCP_HOST="127.0.0.1"
$env:MCP_PORT="8000"
$env:MCP_PATH="/mcp"
knowledge-mcp
```

The local MCP URL is:

```text
http://127.0.0.1:8000/mcp
```

The HTTP transport uses the MCP SDK's DNS-rebinding protection. By default only loopback
Host and Origin values are allowed, and the server binds to `127.0.0.1`. Non-loopback use
requires an explicitly configured allowlist or supplied `TransportSecuritySettings`. This
endpoint has no authentication or TLS and is not intended for direct public Internet access.

The server exposes exactly two read-only tools:

- `search_knowledge`: searches indexed knowledge in `fast` or `quality` mode
- `get_document`: reads the canonical original document through its registered source adapter

`search_knowledge` never indexes or writes data. `get_document` accepts logical
`source_id`/`source_path` values from search results, not arbitrary operating-system paths.
The server process creates one `KnowledgeService`; the reranker remains lazy and is reused
after the first `quality` search.

## Conversation core

V3.0 adds a provider- and client-independent conversation core with persistent messages,
rolling summaries, proposal intent handling and pending proposals. It has no real LLM or
messenger provider yet and cannot write canonical knowledge. See
[docs/conversation-architecture.md](docs/conversation-architecture.md).

Conversation history, summaries, proposal suggestions and proposals share PostgreSQL with
the retrieval index, but they are **durable, backup-relevant application data**. Only the
`chunks` and `index_metadata` tables are disposable. `knowledge index` never modifies the
durable tables, and `knowledge init-db` creates them additively without resetting their data.
Manual `pg_dump`/`pg_restore` recovery and the isolated PostgreSQL verification procedure are
documented in [docs/DEPLOYMENT_DEBIAN.md](docs/DEPLOYMENT_DEBIAN.md#durable-state-backup).

For protocol-level local smoke tests covering stdio and Streamable HTTP:

```bash
pytest tests/test_mcp_server.py
```

Before a real search smoke test, start and initialize the PostgreSQL schema and disposable
retrieval index as in the quick start. Then call `search_knowledge` and `get_document` from a standard MCP client
configured to launch the `knowledge-mcp` command.

## Debian Docker deployment

V2.2 provides a separate [server Compose file](compose.server.yml) so the existing
PostgreSQL-only Windows development workflow remains unchanged. The server deployment runs
PostgreSQL and the read-only MCP service as containers, mounts `knowledge/` read-only from
the checkout, and persists both PostgreSQL data and the Hugging Face model cache.

The MCP process listens on `0.0.0.0:8000` inside its container, while Docker publishes it
only as `127.0.0.1:8000` on the Debian host. It is not configured for public access.

The complete first-install, smoke-test, restart, persistence and update procedure is in
[docs/DEPLOYMENT_DEBIAN.md](docs/DEPLOYMENT_DEBIAN.md).

The reviewed continuation state and the next repository-only Codex Cloud task are recorded in
[docs/CODEX_CLOUD_HANDOFF.md](docs/CODEX_CLOUD_HANDOFF.md).

Machine-readable output:

```bash
knowledge eval --suite eval/retrieval_v01.jsonl --json
```

The eval suite is JSON Lines. Each case contains an `id`, a natural-language `query`,
`expected_sources`, optional `expected_headings`, and an optional `description`.
V0.1 includes a small economics-focused test corpus and retrieval suite. The metrics are
useful as a local baseline, not as a broad benchmark for general knowledge retrieval.
Keyword eval currently supports PostgreSQL `german` and `simple` text search configurations;
`german` is the default because it performs better on the current German-language suite.

## What the current system intentionally does not do

- no ChatGPT integration
- no automatic knowledge writes
- no pull-request workflow
- no HNSW index
- no journal adapter

These remain outside the current read-only local scope.
