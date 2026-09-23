# Repository instructions

## Purpose

This repository is the canonical, versioned store for a personal knowledge system.

## Non-negotiable rules

- Treat files under `knowledge/` as canonical human-reviewable knowledge.
- Never silently delete or rewrite an existing thesis to change its meaning.
- Preserve provenance and revision history where present.
- Separate facts, interpretations, hypotheses, forecasts and open questions.
- Never treat the vector database as the source of truth.
- Derived data must always be reproducible from original sources.
- Do not write automatically to `main`; knowledge changes must be reviewable.
- Prefer small, explicit changes over broad rewrites.
- Avoid duplicate theses. Search for related material before proposing new knowledge.
- When a thesis changes materially, retain enough revision context to explain what changed and why.

## Code changes

- Keep existing relevant comments.
- Add debug logging/prints where operationally useful.
- Prefer clear modules and explicit data flow.
- New retrieval features require tests.
- Index-format changes must remain rebuildable from canonical sources.
