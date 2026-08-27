# Knowledge System

Local-first prototype for a long-term personal knowledge base.

## Core rule

**Original data is the source of truth. The vector database is a disposable index.**

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
MCP Client -> MCP Server -----+
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
PostgreSQL + pgvector / full-text indexes (disposable retrieval cache)
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

The server exposes exactly two read-only tools:

- `search_knowledge`: searches indexed knowledge in `fast` or `quality` mode
- `get_document`: reads the canonical original document through its registered source adapter

`search_knowledge` never indexes or writes data. `get_document` accepts logical
`source_id`/`source_path` values from search results, not arbitrary operating-system paths.
The server process creates one `KnowledgeService`; the reranker remains lazy and is reused
after the first `quality` search.

For a protocol-level local smoke test that starts the stdio subprocess and lists its tools:

```bash
pytest tests/test_mcp_server.py -k stdio
```

Before a real search smoke test, start and initialize the disposable PostgreSQL index as in
the quick start. Then call `search_knowledge` and `get_document` from a standard MCP client
configured to launch the `knowledge-mcp` command.

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
