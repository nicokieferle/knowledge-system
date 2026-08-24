from __future__ import annotations

import argparse
import sys

from .config import get_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knowledge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create pgvector extension and index tables")
    subparsers.add_parser("index", help="Incrementally index Markdown knowledge")

    search_parser = subparsers.add_parser("search", help="Semantic search over indexed knowledge")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=5)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()

    print(f"[config] knowledge_root={settings.knowledge_root}")

    if args.command == "init-db":
        from .db import init_db

        init_db(settings)
        return

    if args.command == "index":
        print(f"[config] embedding_model={settings.embedding_model}")
        from .indexer import index_knowledge

        index_knowledge(settings)
        return

    if args.command == "search":
        print(f"[config] embedding_model={settings.embedding_model}")
        from .search import semantic_search

        results = semantic_search(settings, args.query, args.limit)
        if not results:
            print("No results.")
            return

        for idx, result in enumerate(results, start=1):
            print(
                f"\n[{idx}] similarity={result.similarity:.4f} "
                f"path={result.source_path} heading={result.heading_path}"
            )
            print(result.content)
        return

    raise RuntimeError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
