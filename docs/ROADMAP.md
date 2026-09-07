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

- [x] internal KnowledgeService boundary
- [x] source adapter abstraction
- [x] structured search/fetch API
- [ ] FastAPI service
- [ ] permissions per source
- [ ] structured logging

## V2 — MCP

- [x] read-only MCP tools
- [x] local stdio transport
- [ ] secure tunnel / supported ChatGPT client
- [x] search_knowledge
- [x] get_document
- [ ] find_related

## V2.1 — local Streamable HTTP

- [x] keep stdio as the default transport
- [x] expose the same two read-only tools over Streamable HTTP
- [x] bind to loopback by default
- [x] enforce explicit Host/Origin allowlists through MCP transport security
- [x] verify a real MCP v2 client over local HTTP
- [ ] deploy to the Debian server

## V2.2 — Debian Docker deployment

- [x] add a non-root CPU container image for the knowledge service
- [x] add a separate Debian server Compose definition
- [x] keep the canonical `knowledge/` checkout mounted read-only
- [x] persist PostgreSQL index data and the Hugging Face model cache
- [x] publish MCP only on the Debian host loopback interface
- [x] document explicit initialization, indexing, smoke tests and updates
- [x] verify the full lifecycle on the target Debian host

## V3.0 — conversation + memory core

- [x] durable conversations and raw messages
- [x] recent-message context and rolling summaries with explicit boundaries
- [x] provider-neutral chat, summarizer, intent and proposal-generator protocols
- [x] direct and semantic proposal intent
- [x] persistent proposal suggestions with atomic, idempotent confirmation
- [x] pending proposal records without knowledge or Git writes
- [x] durable-state backup and isolated restore verification on the Debian target host

## V3.1 — client and LLM integration

- [ ] implement one real LLM provider behind the existing protocols
- [ ] add a Telegram adapter as a client of ConversationService

## V3.2 — proposal review

- [ ] proposed / accepted / rejected / deferred lifecycle
- [ ] diff preview
- [ ] stale-base detection
- [ ] explicit approve/reject operations

## V3.3 — safe Git apply

- [ ] isolated Git branch generation
- [ ] guarded canonical knowledge writes after approval
- [ ] reindex after reviewed merge

## V4 — additional sources

- [ ] journal SQLCipher adapter
- [ ] project repositories
- [ ] documents / PDFs
# V3.1 — real chat, Telegram and topic routing

- [x] Real OpenAI-compatible LLM provider
- [x] Thin Telegram long-polling adapter
- [x] Persistent generic client/conversation mapping
- [x] Multi-topic `ConversationRouter`
- [x] Manual `/new`, `/topics`, `/switch`
- [x] Normal chat, isolated memory and direct knowledge retrieval
- [x] Proposal intent and inline suggestion confirmation

V3.2 proposal review UI/lifecycle and V3.3 approved Git/Markdown writes remain open.
