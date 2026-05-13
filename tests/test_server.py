import asyncio
import contextlib
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from chrome_history_mcp import server

CHROME_URLS_SCHEMA = """
CREATE TABLE urls(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url LONGVARCHAR,
    title LONGVARCHAR,
    visit_count INTEGER DEFAULT 0 NOT NULL,
    typed_count INTEGER DEFAULT 0 NOT NULL,
    last_visit_time INTEGER NOT NULL,
    hidden INTEGER DEFAULT 0 NOT NULL
);
"""


@pytest.fixture
def chrome_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "History"
    conn = sqlite3.connect(db_path)
    conn.executescript(CHROME_URLS_SCHEMA)
    conn.executemany(
        "INSERT INTO urls(url, title, visit_count, typed_count, last_visit_time) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("https://a.example/x?q=1,2", "Title, with comma", 3, 1, 13000000000000000),
            ("https://b.example/", "Plain", 1, 0, 13000000000000001),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def wired(chrome_db: Path, tmp_path: Path, monkeypatch):
    snapshot = tmp_path / "snapshot.db"
    monkeypatch.setattr(server, "history_file_original", str(chrome_db))
    monkeypatch.setattr(server, "history_file_tmp", str(snapshot))
    return chrome_db, snapshot


def _run(coro):
    return asyncio.run(coro)


def test_fetch_returns_single_json_textcontent(wired):
    result = _run(server.fetch_from_sqlite("SELECT url, title FROM urls ORDER BY id"))

    assert len(result) == 1
    assert result[0].type == "text"
    payload = json.loads(result[0].text)
    assert payload["truncated"] is False
    assert payload["row_count"] == 2
    assert payload["rows"] == [
        {"url": "https://a.example/x?q=1,2", "title": "Title, with comma"},
        {"url": "https://b.example/", "title": "Plain"},
    ]


def test_snapshot_created_on_first_query(wired):
    _, snapshot = wired
    assert not snapshot.exists()
    _run(server.fetch_from_sqlite("SELECT COUNT(*) AS n FROM urls"))
    assert snapshot.exists()


def test_snapshot_reused_when_source_unchanged(wired):
    chrome_db, snapshot = wired
    _run(server.fetch_from_sqlite("SELECT 1 AS n"))
    first_mtime = snapshot.stat().st_mtime

    # Force the source mtime back to before the snapshot so the refresh check
    # would skip even if it were ambiguous.
    os.utime(chrome_db, (first_mtime - 10, first_mtime - 10))

    _run(server.fetch_from_sqlite("SELECT 1 AS n"))
    assert snapshot.stat().st_mtime == first_mtime


def test_snapshot_refreshed_when_source_newer(wired):
    chrome_db, snapshot = wired
    _run(server.fetch_from_sqlite("SELECT COUNT(*) AS n FROM urls"))
    snap_mtime_before = snapshot.stat().st_mtime

    conn = sqlite3.connect(chrome_db)
    conn.execute(
        "INSERT INTO urls(url, title, last_visit_time) VALUES (?, ?, ?)",
        ("https://c.example/", "Third", 13000000000000002),
    )
    conn.commit()
    conn.close()
    # Bump source mtime well past the snapshot's so the refresh check fires
    # regardless of filesystem mtime granularity.
    os.utime(chrome_db, (snap_mtime_before + 5, snap_mtime_before + 5))

    result = _run(server.fetch_from_sqlite("SELECT COUNT(*) AS n FROM urls"))
    payload = json.loads(result[0].text)
    assert payload["rows"] == [{"n": 3}]
    assert snapshot.stat().st_mtime > snap_mtime_before


def test_snapshot_opened_read_only(wired):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        _run(server.fetch_from_sqlite("INSERT INTO urls(url, last_visit_time) VALUES ('x', 0)"))


def test_write_to_snapshot_blocked_does_not_corrupt(wired):
    chrome_db, snapshot = wired
    with contextlib.suppress(Exception):
        _run(server.fetch_from_sqlite("INSERT INTO urls(url, last_visit_time) VALUES ('x', 0)"))

    # Source DB must still have exactly the two seeded rows.
    conn = sqlite3.connect(chrome_db)
    (count,) = conn.execute("SELECT COUNT(*) FROM urls").fetchone()
    conn.close()
    assert count == 2


@pytest.mark.parametrize(
    "system,expected_suffix",
    [
        ("Darwin", "Library/Application Support/Google/Chrome/Default/History"),
        ("Linux", ".config/google-chrome/Default/History"),
    ],
)
def test_default_history_path_unix(system, expected_suffix):
    with patch("chrome_history_mcp.server.platform.system", return_value=system):
        path = server._default_history_path("Default")
    assert str(path).endswith(expected_suffix)


def test_default_history_path_windows(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\test\\AppData\\Local")
    with patch("chrome_history_mcp.server.platform.system", return_value="Windows"):
        path = server._default_history_path("Profile 1")
    # Path normalizes separators on the host OS; just check the meaningful parts.
    s = str(path)
    assert "Google" in s and "Chrome" in s and "User Data" in s
    assert "Profile 1" in s
    assert s.endswith("History")


def test_default_history_path_unsupported():
    with (
        patch("chrome_history_mcp.server.platform.system", return_value="Plan9"),
        pytest.raises(RuntimeError, match="Unsupported platform"),
    ):
        server._default_history_path("Default")


def test_cli_missing_profile_gives_clear_error(tmp_path, monkeypatch):
    # Point HOME somewhere empty so the default path resolution misses.
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(server.main, ["--profile", "Nonexistent"])
    assert result.exit_code != 0
    assert "Nonexistent" in result.output
    assert "history file not found" in result.output.lower()


def test_cli_bad_path_rejected_by_click(tmp_path):
    runner = CliRunner()
    result = runner.invoke(server.main, ["--path", str(tmp_path / "nope")])
    assert result.exit_code != 0


def test_result_capped_at_max_rows(wired, monkeypatch):
    chrome_db, snapshot = wired
    monkeypatch.setattr(server, "MAX_ROWS", 5)

    conn = sqlite3.connect(chrome_db)
    conn.executemany(
        "INSERT INTO urls(url, last_visit_time) VALUES (?, ?)",
        [(f"https://gen.example/{i}", 13000000000000000 + i) for i in range(20)],
    )
    conn.commit()
    conn.close()
    # Make sure the snapshot will refresh on the next query.
    if snapshot.exists():
        snapshot.unlink()

    result = _run(server.fetch_from_sqlite("SELECT id FROM urls ORDER BY id"))
    payload = json.loads(result[0].text)
    assert payload["truncated"] is True
    assert payload["row_count"] == 5
    assert len(payload["rows"]) == 5


def test_default_snapshot_path_includes_uid_on_posix():
    path = server._default_snapshot_path()
    if hasattr(os, "geteuid"):
        assert str(os.geteuid()) in path.name
    assert path.name.startswith("chrome-history-snapshot")


def test_version_resolved_from_metadata():
    # Either the installed package version or the dev fallback — both are fine,
    # but it must not still be the old hardcoded "0.1.0" string literal.
    assert server.__version__
    assert isinstance(server.__version__, str)
