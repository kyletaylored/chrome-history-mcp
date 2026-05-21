import json
import logging
import os
import platform
import sqlite3
import sys
import tempfile
from contextlib import closing
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import anyio
import click
import mcp.types as types
from mcp.server.lowlevel import Server

logger = logging.getLogger("chrome-history-mcp")

MAX_ROWS = 1000

try:
    __version__ = _pkg_version("chrome-history-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"


def _default_snapshot_path() -> Path:
    base = Path(tempfile.gettempdir())
    if hasattr(os, "geteuid"):
        return base / f"chrome-history-snapshot-{os.geteuid()}"
    return base / "chrome-history-snapshot"


history_file_original = None
history_file_tmp = str(_default_snapshot_path())


def _refresh_snapshot() -> None:
    src_mtime = os.stat(history_file_original).st_mtime
    try:
        tmp_mtime = os.stat(history_file_tmp).st_mtime
        needs_copy = src_mtime > tmp_mtime
    except FileNotFoundError:
        needs_copy = True

    if not needs_copy:
        logger.debug("Reusing snapshot at %s", history_file_tmp)
        return

    logger.debug("Snapshotting %s -> %s", history_file_original, history_file_tmp)
    # Open the source with immutable=1: Chrome holds a write lock that blocks
    # SQLite's backup API from getting a read transaction, causing backup() to
    # spin in SQLITE_BUSY forever. immutable=1 tells SQLite to skip locking
    # entirely. Trade-off: anything still in Chrome's WAL that hasn't been
    # checkpointed is invisible to us — fine for history reconstruction.
    src_uri = Path(history_file_original).as_uri() + "?immutable=1"
    with closing(sqlite3.connect(src_uri, uri=True)) as src, \
            closing(sqlite3.connect(history_file_tmp)) as dst:
        src.backup(dst)


async def fetch_from_sqlite(
    sql_statement: str,
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    try:
        _refresh_snapshot()
    except Exception as e:
        raise RuntimeError(f"Failed to snapshot Chrome history: {e}") from e

    snapshot_uri = Path(history_file_tmp).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(snapshot_uri, uri=True)) as conn:
        c = conn.cursor()
        c.execute(sql_statement)
        column_names = [desc[0] for desc in c.description]
        rows = []
        truncated = False
        for i, row in enumerate(c):
            if i >= MAX_ROWS:
                truncated = True
                break
            rows.append(dict(zip(column_names, row, strict=False)))

    payload = {"row_count": len(rows), "truncated": truncated, "rows": rows}
    return [types.TextContent(type="text", text=json.dumps(payload, default=str))]


def _default_history_path(profile: str) -> Path:
    system = platform.system().lower()
    if system == "windows":
        return (
            Path(os.getenv("LOCALAPPDATA", ""))
            / "Google"
            / "Chrome"
            / "User Data"
            / profile
            / "History"
        )
    if system == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / profile
            / "History"
        )
    if system == "linux":
        return Path.home() / ".config" / "google-chrome" / profile / "History"
    raise RuntimeError(f"Unsupported platform: {system}")


@click.command()
@click.option(
    "--path",
    required=False,
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Full path to the Chrome History sqlite file. Overrides --profile.",
)
@click.option(
    "--profile",
    required=False,
    default="Default",
    show_default=True,
    help='Chrome profile directory name (e.g. "Default", "Profile 1"). Ignored if --path is provided.',
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Log snapshot activity and path resolution to stderr.",
)
def main(path: Path | None, profile: str, verbose: bool) -> int:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = Server("chrome-history-mcp", version=__version__)

    @app.call_tool()
    async def fetch_tool(
        name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        if name != "query-chrome-history":
            raise ValueError(f"Unknown tool: {name}")
        if "sql_statement" not in arguments:
            raise ValueError("Missing required argument 'sql_statement'")
        return await fetch_from_sqlite(sql_statement=arguments["sql_statement"])

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="query-chrome-history",
                description=(
                    "Run a read-only SQL query against a snapshot of Chrome's History DB. "
                    "Connection is opened read-only — INSERT/UPDATE/DELETE will fail. "
                    f"Results are capped at {MAX_ROWS} rows; the payload includes "
                    "`row_count` and a `truncated` flag. "
                    "Timestamps (last_visit_time, visit_time) use Chrome's WebKit format: "
                    "microseconds since 1601-01-01 UTC — NOT Unix epoch. "
                    "Always convert them in your SQL using: "
                    "datetime(column / 1000000 - 11644473600, 'unixepoch') "
                    "(11644473600 is the number of seconds between 1601-01-01 and 1970-01-01). "
                    "Example: SELECT url, datetime(last_visit_time/1000000 - 11644473600, 'unixepoch') "
                    "AS last_visit FROM urls ORDER BY last_visit_time DESC LIMIT 10;\n\n"
                    "Available tables:\n\n"
                    "CREATE TABLE urls(id INTEGER PRIMARY KEY AUTOINCREMENT, url LONGVARCHAR, "
                    "title LONGVARCHAR, visit_count INTEGER DEFAULT 0 NOT NULL, "
                    "typed_count INTEGER DEFAULT 0 NOT NULL, last_visit_time INTEGER NOT NULL, "
                    "hidden INTEGER DEFAULT 0 NOT NULL);\n"
                    "CREATE INDEX urls_url_index ON urls (url);\n\n"
                    "CREATE TABLE visits(id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER NOT NULL, "
                    "visit_time INTEGER NOT NULL, from_visit INTEGER, transition INTEGER DEFAULT 0 NOT NULL, "
                    "segment_id INTEGER, visit_duration INTEGER DEFAULT 0 NOT NULL, "
                    "incremented_omnibox_typed_score BOOLEAN DEFAULT FALSE NOT NULL, "
                    "opener_visit INTEGER, originator_cache_guid TEXT, originator_visit_id INTEGER, "
                    "originator_from_visit INTEGER, originator_opener_visit INTEGER, "
                    "is_known_to_sync BOOLEAN DEFAULT FALSE NOT NULL, "
                    "consider_for_ntp_most_visited BOOLEAN DEFAULT FALSE NOT NULL, "
                    "external_referrer_url TEXT, visited_link_id INTEGER, app_id TEXT);\n"
                    "CREATE INDEX visits_url_index ON visits (url);\n"
                    "CREATE INDEX visits_from_index ON visits (from_visit);\n"
                    "CREATE INDEX visits_time_index ON visits (visit_time);\n"
                    "CREATE INDEX visits_originator_id_index ON visits (originator_visit_id);"
                ),
                inputSchema={
                    "type": "object",
                    "required": ["sql_statement"],
                    "properties": {
                        "sql_statement": {
                            "type": "string",
                            "description": "Read-only SQL statement to execute against the snapshot.",
                        }
                    },
                },
            ),
        ]

    if path is None:
        path = _default_history_path(profile)
        logger.debug("Resolved history path for profile %r: %s", profile, path)

    if not path.exists():
        raise click.ClickException(
            f"Chrome history file not found at {path}. "
            f"Check that profile {profile!r} exists, or pass --path explicitly."
        )

    global history_file_original
    history_file_original = str(path)
    logger.debug("Using snapshot path %s", history_file_tmp)

    from mcp.server.stdio import stdio_server

    async def arun():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream, write_stream, app.create_initialization_options()
            )

    anyio.run(arun)

    return 0
