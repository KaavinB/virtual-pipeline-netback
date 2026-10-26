"""Apply sql/migrations/NNN_*.sql in order, exactly once each.

Applied files are recorded in public.schema_migrations together with a SHA-256 of their
contents. Editing a migration that has already been applied is refused: change the schema
with a new migration instead, so every database reaches the same state by the same path.
"""

import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ingestion.config import ROOT, setup_logging
from ingestion.db import connect

log = logging.getLogger("migrate")
MIGRATIONS_DIR = ROOT / "sql" / "migrations"
FILENAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = FILENAME.match(path.name)
        if not match:
            raise ValueError(f"bad migration filename: {path.name} (want NNN_snake_case.sql)")
        migrations.append(Migration(int(match.group(1)), path.name, path.read_text()))
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration version in {versions}")
    return migrations


def pending(migrations: list[Migration], applied: dict[int, str]) -> list[Migration]:
    """Return unapplied migrations; fail loudly if an applied file has been edited since."""
    for m in migrations:
        if m.version in applied and applied[m.version] != m.checksum:
            raise RuntimeError(f"{m.name} was modified after being applied; add a new migration")
    return [m for m in migrations if m.version not in applied]


def main() -> int:
    setup_logging()
    # autocommit, so each `conn.transaction()` below is a real, independent transaction
    with connect(autocommit=True) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS public.schema_migrations (
                   version    int PRIMARY KEY,
                   name       text NOT NULL,
                   checksum   text NOT NULL,
                   applied_at timestamptz NOT NULL DEFAULT now())"""
        )
        applied = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
        todo = pending(discover(), applied)
        for m in todo:
            # one transaction per file: a failing migration leaves no half-applied schema
            with conn.transaction():
                conn.execute(m.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
            log.info("applied %s", m.name)
        log.info("schema up to date (%d applied, %d new)", len(applied) + len(todo), len(todo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
