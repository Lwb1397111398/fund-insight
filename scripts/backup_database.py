"""Create a verified local SQLite snapshot without reading DATABASE_URL."""

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_counts(connection: sqlite3.Connection) -> Dict[str, int]:
    table_names = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    counts = {}
    for (table_name,) in table_names:
        quoted_name = table_name.replace('"', '""')
        counts[table_name] = connection.execute(
            f'SELECT COUNT(*) FROM "{quoted_name}"'
        ).fetchone()[0]
    return counts


def create_sqlite_backup(
    source_path: Path,
    output_dir: Path,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Path]:
    source_path = Path(source_path).resolve()
    output_dir = Path(output_dir).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    created_at = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    created_at = created_at.astimezone(timezone.utc)
    backup_id = created_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    backup_path = output_dir / f"fund_insight_{backup_id}.sqlite3"
    manifest_path = output_dir / f"fund_insight_{backup_id}.manifest.json"
    if backup_path.exists() or manifest_path.exists():
        raise FileExistsError(f"Backup already exists for {backup_id}")

    source_uri = source_path.as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(backup_path) as destination:
                source.backup(destination)
                destination.commit()
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise

    try:
        with sqlite3.connect(backup_path) as snapshot:
            integrity = snapshot.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {integrity}")
            tables = _table_counts(snapshot)

        manifest = {
            "format_version": 1,
            "backup_id": backup_id,
            "created_at": created_at.isoformat(),
            "source_type": "sqlite",
            "database_file": backup_path.name,
            "size_bytes": backup_path.stat().st_size,
            "sha256": _sha256(backup_path),
            "integrity_check": "ok",
            "tables": tables,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        backup_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise

    return {"backup_path": backup_path, "manifest_path": manifest_path}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a verified local SQLite backup")
    parser.add_argument("--sqlite-path", default="data/fund_insight.db")
    parser.add_argument("--output-dir", default="backup")
    args = parser.parse_args()
    result = create_sqlite_backup(Path(args.sqlite_path), Path(args.output_dir))
    print(f"Backup: {result['backup_path']}")
    print(f"Manifest: {result['manifest_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
