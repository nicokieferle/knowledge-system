from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

EXPECTED_TOOLS = ["search_knowledge", "get_document"]
DEFAULT_QUERY = "Wie können hohe Staatsschulden die Geldpolitik beeinflussen?"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test the read-only MCP HTTP endpoint")
    parser.add_argument(
        "url",
        nargs="?",
        default="http://127.0.0.1:8000/mcp",
    )
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--timeout", type=float, default=1200.0)
    return parser.parse_args()


def _access_headers() -> dict[str, str]:
    token = os.getenv("MCP_HTTP_BEARER_TOKEN", "")
    if not token:
        raise RuntimeError("MCP_HTTP_BEARER_TOKEN is required for the MCP HTTP smoke test")
    client_id = os.getenv("CF_ACCESS_CLIENT_ID")
    client_secret = os.getenv("CF_ACCESS_CLIENT_SECRET")

    if bool(client_id) != bool(client_secret):
        raise RuntimeError(
            "CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET "
            "must either both be set or both be unset"
        )

    headers = {"Authorization": f"Bearer {token}"}
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret
    return headers


def _require_result(result: Any, tool_name: str) -> dict[str, Any]:
    if result.is_error:
        messages = [getattr(block, "text", "") for block in result.content]
        raise RuntimeError(f"{tool_name} failed: {' '.join(messages)}")
    return result.structured_content or {}


async def _run(
    url: str,
    query: str,
    timeout: float,
) -> dict[str, Any]:
    headers = _access_headers()

    async with httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(30.0, read=timeout),
        follow_redirects=True,
    ) as http_client:
        transport = streamable_http_client(
            url,
            http_client=http_client,
        )

        async with Client(transport, mode="legacy") as client:
            listed = await client.list_tools()
            tool_names = [tool.name for tool in listed.tools]

            if tool_names != EXPECTED_TOOLS:
                raise RuntimeError(f"Unexpected MCP tools: {tool_names}")

            fast_started = time.perf_counter()
            fast = await client.call_tool(
                "search_knowledge",
                {
                    "query": query,
                    "mode": "fast",
                    "limit": 5,
                },
            )
            fast_seconds = time.perf_counter() - fast_started
            fast_content = _require_result(
                fast,
                "search_knowledge fast",
            )
            fast_results = fast_content.get("results", [])

            if not fast_results:
                raise RuntimeError("search_knowledge fast returned no results")

            quality_times: list[float] = []
            quality_results: list[dict[str, Any]] = []

            for _ in range(2):
                quality_started = time.perf_counter()
                quality = await client.call_tool(
                    "search_knowledge",
                    {
                        "query": query,
                        "mode": "quality",
                        "limit": 5,
                    },
                )
                quality_times.append(time.perf_counter() - quality_started)
                quality_content = _require_result(
                    quality,
                    "search_knowledge quality",
                )
                quality_results = quality_content.get(
                    "results",
                    [],
                )

                if not quality_results:
                    raise RuntimeError("search_knowledge quality returned no results")

            first_hit = quality_results[0]

            document = await client.call_tool(
                "get_document",
                {
                    "source_id": first_hit["source_id"],
                    "source_path": first_hit["source_path"],
                },
            )
            document_content = _require_result(
                document,
                "get_document",
            )

    return {
        "url": url,
        "machine_auth": True,
        "cloudflare_access": "CF-Access-Client-Id" in headers,
        "tools": tool_names,
        "fast": {
            "seconds": round(fast_seconds, 3),
            "results": len(fast_results),
            "top_source": fast_results[0]["source_path"],
        },
        "quality": {
            "first_seconds": round(quality_times[0], 3),
            "second_seconds": round(quality_times[1], 3),
            "results": len(quality_results),
            "top_source": first_hit["source_path"],
        },
        "document": {
            "source_id": document_content["source_id"],
            "source_path": document_content["source_path"],
            "content_characters": len(document_content["content"]),
        },
    }


def main() -> None:
    args = _parse_args()
    result = asyncio.run(_run(args.url, args.query, args.timeout))
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
