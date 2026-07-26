import hashlib
import json
import sqlite3
from datetime import datetime, timezone


def test_sqlite_backup_creates_verified_snapshot_and_safe_manifest(tmp_path):
    from scripts.backup_database import create_sqlite_backup

    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, status TEXT)")
        connection.executemany(
            "INSERT INTO predictions(status) VALUES (?)",
            [("pending",), ("success",), ("failed",)],
        )
        connection.commit()

    output = tmp_path / "backups"
    result = create_sqlite_backup(
        source,
        output,
        now=datetime(2026, 7, 22, 12, 30, tzinfo=timezone.utc),
    )

    backup_path = result["backup_path"]
    manifest_path = result["manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 3

    expected_hash = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert manifest["sha256"] == expected_hash
    assert manifest["tables"]["predictions"] == 3
    assert manifest["source_type"] == "sqlite"
    assert str(source) not in serialized
    assert "DATABASE_URL" not in serialized
