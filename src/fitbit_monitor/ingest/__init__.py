"""Getting data in: idempotent ingest, watermarks, the ad-hoc import, and the CSV fallback.

The pieces, biggest first:

* :mod:`fitbit_monitor.ingest.importer` - ``fm import``'s engine: plan a window, fetch
  it in chunks, commit per chunk, advance watermarks, report.
* :mod:`fitbit_monitor.ingest.csv_source` - the same ingest path fed from a Fitbit
  Export Data / Google Takeout archive.
* :mod:`fitbit_monitor.ingest.pipeline` - :class:`~fitbit_monitor.ingest.pipeline.Ingest`:
  raw payload retention with content-hash dedup, row upserts, quarantine.
* :mod:`fitbit_monitor.ingest.report` - the JSON report and the exit codes.
* :mod:`fitbit_monitor.ingest.runlog` - one ``dataset_run`` row per dataset per run.

Watermarks themselves live with the store (``fitbit_monitor.store.watermarks``): they
are storage state, not import state.
"""

from __future__ import annotations

from ..store.watermarks import Watermark, Watermarks
from .csv_source import CsvImport, ParsedFile, parse_export
from .importer import ChunkPlan, DatasetPlan, ImportPlan, ImportPlanner, Importer, day_window
from .pipeline import Chunk, Ingest, IngestStats
from .report import DatasetReport, ImportReport
from .runlog import DatasetRun, RunLog

__all__ = [
    "Chunk",
    "ChunkPlan",
    "CsvImport",
    "DatasetPlan",
    "DatasetReport",
    "DatasetRun",
    "ImportPlan",
    "ImportPlanner",
    "ImportReport",
    "Importer",
    "Ingest",
    "IngestStats",
    "ParsedFile",
    "RunLog",
    "Watermark",
    "Watermarks",
    "day_window",
    "parse_export",
]
