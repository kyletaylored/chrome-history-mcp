"""Time a single fetch_from_sqlite call against the real Chrome History DB.

Prints every step so we can see exactly where it hangs. Run with:

    uv run python scripts/time_query.py [profile]

Default profile is "Default". Useful for diagnosing snapshot/backup hangs
without going through Inspector or the MCP transport.
"""

import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from chrome_history_mcp import server


def log(msg: str) -> None:
    print(f"[{time.time():.2f}] {msg}", flush=True)


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "Default"
    CHROME_HISTORY = server._default_history_path(profile)
    log(f"python    {sys.version.split()[0]}")
    log(f"sqlite    {sqlite3.sqlite_version}")
    log(f"history   {CHROME_HISTORY}")
    log(f"exists?   {CHROME_HISTORY.exists()}")
    if CHROME_HISTORY.exists():
        size_mb = CHROME_HISTORY.stat().st_size / 1024 / 1024
        log(f"size      {size_mb:.1f} MB")

    snap = Path(server.history_file_tmp)
    log(f"snapshot  {snap}")
    log(f"exists?   {snap.exists()}")
    if snap.exists():
        log(f"size      {snap.stat().st_size / 1024 / 1024:.1f} MB")
        log("removing stale snapshot")
        snap.unlink()

    src_uri = CHROME_HISTORY.as_uri() + "?immutable=1"
    log(f"connecting to source ({src_uri})…")
    src = sqlite3.connect(src_uri, uri=True)
    log("connected to source")

    log("connecting to destination…")
    dst = sqlite3.connect(str(snap))
    log("connected to destination")

    log("starting backup (page-by-page, 100 pages per step)…")
    pages_done = 0

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal pages_done
        pages_done = total - remaining
        log(f"  backup progress: {pages_done}/{total} pages")

    try:
        src.backup(dst, pages=100, progress=progress, sleep=0.1)
        log("backup complete")
    except Exception as e:
        log(f"backup raised: {type(e).__name__}: {e}")
        raise
    finally:
        with closing(dst):
            pass
        with closing(src):
            pass

    log(f"snapshot size: {snap.stat().st_size / 1024 / 1024:.1f} MB")

    log("querying snapshot…")
    server.history_file_original = str(CHROME_HISTORY)
    import asyncio

    start = time.time()
    result = asyncio.run(server.fetch_from_sqlite("SELECT COUNT(*) AS n FROM urls"))
    log(f"query roundtrip: {time.time() - start:.2f}s")
    log(f"result: {result[0].text[:200]}")


if __name__ == "__main__":
    main()
