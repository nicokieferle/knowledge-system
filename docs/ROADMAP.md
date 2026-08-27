# Roadmap

## V0 — retrieval skeleton

- [x] Git/Markdown source of truth
- [x] PostgreSQL + pgvector
- [x] heading-aware Markdown chunking
- [x] local multilingual embeddings
- [x] content hashes
- [x] incremental re-embedding
- [x] exact cosine search
- [x] basic tests

## V0.1 — retrieval quality

- [x] define a small extensible retrieval eval format
- [x] add a seed eval suite for the current knowledge base
- [x] add Hit@1 / Hit@3 / Hit@5 / MRR baseline runner
- [x] expand the retrieval test set to 20–50 questions once enough real knowledge exists
- [x] add PostgreSQL full-text keyword search
- [x] combine keyword + vector results with Reciprocal Rank Fusion
- [x] test keyword-candidate reranking with a multilingual cross-encoder
- [ ] add metadata/frontmatter parsing
- [x] measure hit@k / MRR before changing models or chunking

## V1 — service layer

- [ ] FastAPI/internal service
- [ ] source adapter abstraction
- [ ] structured search/fetch API
- [ ] permissions per source
- [ ] structured logging

## V2 — MCP

- [ ] read-only MCP tools
- [ ] secure tunnel / supported ChatGPT client
- [ ] search_knowledge
- [ ] get_document
- [ ] find_related

## V3 — proposals

- [ ] proposal table
- [ ] proposed / accepted / rejected / deferred
- [ ] Git branch generation
- [ ] diff preview
- [ ] stale-base detection
- [ ] approve/reject operations
- [ ] reindex after merge

## V4 — additional sources

- [ ] journal SQLCipher adapter
- [ ] project repositories
- [ ] documents / PDFs
