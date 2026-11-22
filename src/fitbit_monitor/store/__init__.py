"""Store access for fitbit-monitor.

Three halves, and the split is the point:

* :class:`~fitbit_monitor.store.writer.SeriesStore` writes (ingest, seeding, alert
  history, pruning).
* :class:`~fitbit_monitor.store.reader.ReadOnlyStore` reads, and is the only thing a
  plugin ever receives. It opens SQLite with ``mode=ro`` so "read-only" is enforced by
  the database rather than by manners.
* :mod:`fitbit_monitor.store.maintenance` proves the copy is intact: stats, verify,
  export, backup, prune.

The schema lives in numbered SQL files under :mod:`fitbit_monitor.store.migrations` and
is applied by :func:`fitbit_monitor.store.schema.migrate_store` on every open (and
explicitly by ``fm db migrate``). See ``docs/storage.md`` and ADR-0003.
"""

from __future__ import annotations

from .datasets import DatasetSpec
from .downsample import aggregate, daily_values, downsample
from .reader import Completeness, Coverage, DatasetInfo, Gap, ReadOnlyStore, SeriesPoint, stored_datasets
from .schema import SCHEMA_VERSION, init_db, migrate_store, schema_version
from .writer import SeriesStore

__all__ = [
    "SCHEMA_VERSION",
    "Completeness",
    "Coverage",
    "DatasetInfo",
    "DatasetSpec",
    "Gap",
    "ReadOnlyStore",
    "SeriesPoint",
    "SeriesStore",
    "aggregate",
    "daily_values",
    "downsample",
    "init_db",
    "migrate_store",
    "schema_version",
    "stored_datasets",
]
