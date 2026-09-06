from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

source_path = Path(
    os.environ.get(
        "HERMES_KANBAN_DB",
        "/var/lib/hermes-kanban/kanban.db",
    )
)

backup_dir = Path("/opt/data/backups/kanban")
backup_dir.mkdir(parents=True, exist_ok=True)

timestamp = datetime.now(timezone.utc).strftime(
    "%Y%m%dT%H%M%SZ"
)

temporary = backup_dir / f".kanban-{timestamp}.tmp"
destination = backup_dir / f"kanban-{timestamp}.db"

if temporary.exists():
    temporary.unlink()

source = sqlite3.connect(
    f"file:{source_path}?mode=ro",
    uri=True,
    timeout=30,
)

target = sqlite3.connect(temporary)

try:
    source.backup(
        target,
        pages=256,
        sleep=0.05,
    )

    result = target.execute(
        "PRAGMA integrity_check"
    ).fetchone()

    if not result or result[0] != "ok":
        raise RuntimeError(
            f"backup integrity check failed: {result}"
        )
finally:
    target.close()
    source.close()

temporary.replace(destination)

backups = sorted(
    backup_dir.glob("kanban-*.db"),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)

for old_backup in backups[14:]:
    old_backup.unlink()

print(f"backup={destination}")
print("integrity=ok")
