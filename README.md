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

Later versions can add hybrid retrieval, MCP, proposals, approval/rejection and other source adapters.

## Architecture

```text
knowledge/*.md (Git)
        |
        v
Markdown chunker
        |
        v
local embedding model
        |
        v
PostgreSQL + pgvector
        |
        v
semantic search
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

## What V0 intentionally does not do

- no MCP
- no ChatGPT integration
- no automatic knowledge writes
- no pull-request workflow
- no reranker
- no HNSW index
- no journal adapter

These come only after retrieval quality is measurable.
