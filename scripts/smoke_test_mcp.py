#!/usr/bin/env python
"""Drive the TaxCite MCP server over stdio the way a real client does.

The in-process tests in ``tests/test_server.py`` call the tool functions directly,
which does not prove the stdio transport works, that the process starts, or that the
tools are actually registered with the protocol. This does.

    uv run python scripts/smoke_test_mcp.py

Equivalent to what happens after::

    claude mcp add taxcite -- uv --directory /path/to/taxcite run taxcite serve

Requires a built index (``uv run taxcite build-index``). Exits non-zero on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "get_irc_provision",
    "get_treasury_reg",
    "search_tax_law",
    "resolve_citation",
    "verify_citations",
    "get_cross_references",
    "find_definition",
    "list_subdivisions",
}


async def main() -> int:
    """Start the server, exercise it, and report."""
    params = StdioServerParameters(
        command="uv",
        args=["run", "taxcite", "serve"],
        env=dict(os.environ),
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        print(f"connected to {init.server_info.name} {init.server_info.version}")

        names = {tool.name for tool in (await session.list_tools()).tools}
        print(f"tools: {', '.join(sorted(names))}")
        missing = EXPECTED_TOOLS - names
        if missing:
            print(f"FAIL: missing tools {sorted(missing)}", file=sys.stderr)
            return 1

        prompts = {prompt.name for prompt in (await session.list_prompts()).prompts}
        print(f"prompts: {', '.join(sorted(prompts))}")
        if "grounded_tax_memo" not in prompts:
            print("FAIL: grounded_tax_memo prompt is missing", file=sys.stderr)
            return 1

        result = await session.call_tool(
            "get_irc_provision", {"citation": "§ 162(a)", "include_children": False}
        )
        text = result.content[0].text
        if "ordinary and necessary expenses" not in text:
            print(f"FAIL: unexpected provision text: {text[:200]}", file=sys.stderr)
            return 1
        print("get_irc_provision returned the real text of I.R.C. § 162(a)")

        result = await session.call_tool("verify_citations", {"text": "See § 162A and § 162(a)."})
        report = result.content[0].text
        if "not_found" not in report:
            print("FAIL: verify_citations did not catch § 162A", file=sys.stderr)
            return 1
        print("verify_citations caught the nonexistent § 162A")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
