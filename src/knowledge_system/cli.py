from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

from .config import get_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knowledge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create pgvector extension and index tables")
    subparsers.add_parser("index", help="Incrementally index Markdown knowledge")

    search_parser = subparsers.add_parser("search", help="Search over indexed knowledge")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=5)
    search_parser.add_argument("--mode", choices=("fast", "quality"), default="quality")

    eval_parser = subparsers.add_parser("eval", help="Run retrieval quality evaluation")
    eval_parser.add_argument("--suite", type=Path, default=Path("eval/retrieval_v01.jsonl"))
    eval_parser.add_argument("--limit", type=int, default=5)
    eval_parser.add_argument(
        "--retriever",
        choices=("vector", "keyword", "hybrid", "reranker"),
        default="vector",
    )
    eval_parser.add_argument("--text-config", choices=("german", "simple"), default="german")
    eval_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    quiet = args.command == "eval" and args.json

    if not quiet:
        print(f"[config] knowledge_root={settings.knowledge_root}")

    if args.command == "init-db":
        from .db import init_db

        init_db(settings)
        return

    if args.command == "index":
        print(f"[config] embedding_model={settings.embedding_model}")
        from .service import KnowledgeService

        KnowledgeService(settings).index()
        return

    if args.command == "search":
        print(f"[search] mode={args.mode}")
        from .service import KnowledgeService

        results = KnowledgeService(settings).search(args.query, mode=args.mode, limit=args.limit)
        if not results:
            print("No results.")
            return

        for idx, result in enumerate(results, start=1):
            print(
                f"\n[{idx}] score={result.score:.4f} source={result.source_id} "
                f"path={result.source_path} heading={result.heading_path}"
            )
            print(result.content)
        return

    if args.command == "eval":
        if not args.json:
            print(f"[config] embedding_model={settings.embedding_model}")
        from .evaluation import format_human_report, run_retrieval_eval

        if args.json:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                report = run_retrieval_eval(
                    settings,
                    args.suite,
                    limit=args.limit,
                    retriever=args.retriever,
                    text_config=args.text_config,
                    verbose=False,
                )
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            report = run_retrieval_eval(
                settings,
                args.suite,
                limit=args.limit,
                retriever=args.retriever,
                text_config=args.text_config,
                verbose=True,
            )
            print(format_human_report(report))
        return

    raise RuntimeError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
