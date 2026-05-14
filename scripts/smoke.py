"""End-to-end smoke test for chrome-history-mcp.

Spawns the server as a stdio subprocess, fires the canonical five queries
via the MCP protocol, and reports pass/fail for each. The pytest suite
covers the data path against a synthetic SQLite; this covers the wire
layer plus a real Chrome history file.

Run with:

    uv run poe smoke
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}✗{RESET} {msg}")


def dim(msg: str) -> None:
    print(f"{DIM}   {msg}{RESET}")


QUERIES = [
    {
        "label": "schema sanity",
        "sql": "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        "expect": "success",
        "verify": lambda p: {"urls", "visits"}.issubset({r["name"] for r in p["rows"]}),
        "verify_desc": "both `urls` and `visits` tables visible",
    },
    {
        "label": "row count",
        "sql": "SELECT COUNT(*) AS n FROM urls",
        "expect": "success",
        "verify": lambda p: p["rows"][0]["n"] >= 0,
        "verify_desc": "returns a non-negative count",
    },
    {
        "label": "recent visits with timestamp conversion",
        "sql": (
            "SELECT url, title, "
            "datetime(last_visit_time/1000000 - 11644473600, 'unixepoch', 'localtime') AS visited "
            "FROM urls ORDER BY last_visit_time DESC LIMIT 5"
        ),
        "expect": "success",
        "verify": lambda p: all("visited" in r for r in p["rows"]),
        "verify_desc": "rows include a `visited` column",
    },
    {
        "label": "read-only guarantee",
        "sql": "INSERT INTO urls(url, last_visit_time) VALUES ('smoke-test', 0)",
        "expect": "error",
        "verify": lambda text: "readonly" in text.lower() or "read-only" in text.lower(),
        "verify_desc": "fails with 'readonly database'",
    },
    {
        "label": "truncation behavior",
        "sql": "SELECT id, url FROM urls",
        "expect": "success",
        "verify": lambda p: p["row_count"] <= 1000,
        "verify_desc": "result capped at <= 1000 rows",
    },
]


async def run_one(session: ClientSession, q: dict) -> bool:
    try:
        result = await session.call_tool(
            "query-chrome-history", {"sql_statement": q["sql"]}
        )
    except Exception as e:
        if q["expect"] == "error" and q["verify"](str(e)):
            ok(f"{q['label']}: {q['verify_desc']}")
            dim(f"raised {type(e).__name__}: {str(e)[:160]}")
            return True
        fail(f"{q['label']}: unexpected exception {type(e).__name__}: {e}")
        return False

    text = result.content[0].text if result.content else ""

    if result.isError:
        if q["expect"] == "error" and q["verify"](text):
            ok(f"{q['label']}: {q['verify_desc']}")
            dim(text[:200])
            return True
        fail(f"{q['label']}: unexpected error response")
        dim(text[:200])
        return False

    if q["expect"] == "error":
        fail(f"{q['label']}: expected error but got success")
        dim(text[:200])
        return False

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        fail(f"{q['label']}: response is not JSON")
        dim(text[:200])
        return False

    if q["verify"](payload):
        ok(f"{q['label']}: {q['verify_desc']}")
        dim(f"row_count={payload['row_count']} truncated={payload['truncated']}")
        return True

    fail(f"{q['label']}: verification failed")
    dim(text[:300])
    return False


async def main() -> int:
    server_params = StdioServerParameters(command="chrome-history-mcp", args=[])
    print(f"Spawning server: {server_params.command}\n")

    async with (
        stdio_client(server_params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        print(f"Server exposes {len(tools.tools)} tool(s): {[t.name for t in tools.tools]}\n")

        results = []
        for q in QUERIES:
            results.append(await run_one(session, q))

        passed = sum(results)
        total = len(results)
        print(f"\n{passed}/{total} passed")
        return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
