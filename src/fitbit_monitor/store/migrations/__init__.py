"""Numbered SQL migrations, applied in order by :func:`fitbit_monitor.store.schema.migrate`.

A migration is a file in this package named ``NNNN_slug.sql``. Files, not Python
strings, because the schema is the part of the program other people have to reason
about: a reviewer should be able to read the next version of the store in SQL, and a
DBA should be able to run it by hand with ``sqlite3`` if they have to.

Rules (ADR-0003):

* **forward-only** - never edit a committed file; add the next number;
* **one transaction per migration** - a migration that fails halfway leaves the
  database at the version it started at, so a crashed `fm db migrate` is safe to re-run;
* **four digits** - the version in the file name is the version recorded in
  ``fm_meta``, which is what makes "this database is from a newer fm" detectable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    """One numbered file, already split into executable statements."""

    version: int
    name: str
    sql: str
    statements: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def split_statements(sql: str) -> tuple[str, ...]:
    """Split a migration file into statements.

    Deliberately small: ``--`` line comments are dropped (nothing in this project puts
    ``--`` inside a string literal) and ``;`` terminates a statement. That is all the
    schema needs; the moment a migration needs a ``BEGIN ... END`` body (a trigger)
    this function gets a real lexer rather than everyone inventing their own delimiter.
    """
    lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.split("--", 1)[0]
        if stripped.strip():
            lines.append(stripped)
    statements = [chunk.strip() for chunk in "\n".join(lines).split(";")]
    return tuple(statement for statement in statements if statement)


def load_migrations() -> tuple[Migration, ...]:
    """Every migration file, in version order. Raises if the numbering is broken."""
    found: list[Migration] = []
    directory = resources.files(__package__)
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        match = _NAME_RE.match(entry.name)
        if match is None:
            continue
        sql = entry.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=sql,
                statements=split_statements(sql),
            )
        )
    if not found:  # pragma: no cover - a broken install, not a user error
        raise RuntimeError("no SQL migrations found in fitbit_monitor.store.migrations")
    versions = [migration.version for migration in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return tuple(sorted(found, key=lambda migration: migration.version))


#: Every migration, loaded once at import: an ImportError-time failure beats a
#: half-migrated database at runtime.
MIGRATIONS = load_migrations()

#: The version a freshly migrated database reaches.
LATEST_VERSION = MIGRATIONS[-1].version


def by_version(version: int) -> Migration:
    for migration in MIGRATIONS:
        if migration.version == version:
            return migration
    raise KeyError(f"no migration {version}")


__all__ = ["LATEST_VERSION", "MIGRATIONS", "Migration", "by_version", "load_migrations", "split_statements"]
