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

- [ ] build a 20–50 question retrieval test set
- [ ] add PostgreSQL full-text keyword search
- [ ] combine keyword + vector results with Reciprocal Rank Fusion
- [ ] add metadata/frontmatter parsing
- [ ] measure hit@k / MRR before changing models or chunking

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
