"""Store access for fitbit-monitor.

Two halves, and the split is the point:

* :class:`~fitbit_monitor.store.writer.SeriesStore` writes (ingest, seeding,
  alert history).
* :class:`~fitbit_monitor.store.reader.ReadOnlyStore` reads, and is the only thing
  a plugin ever receives. It opens SQLite with ``mode=ro`` so "read-only" is
  enforced by the database rather than by manners.

The surface is intentionally narrow and shaped like the storage epic's read API
(range iterators, coverage, downsample helpers) so the plugin layer does not have
to change when ``fitb-ei6`` grows the full schema behind it.
"""

from __future__ import annotations

from .downsample import aggregate, downsample
from .reader import Coverage, ReadOnlyStore, SeriesPoint
from .writer import SeriesStore

__all__ = [
    "Coverage",
    "ReadOnlyStore",
    "SeriesPoint",
    "SeriesStore",
    "aggregate",
    "downsample",
]
