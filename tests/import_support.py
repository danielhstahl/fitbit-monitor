"""Shared helpers for the import tests: a replay-backed client and a progress sink.

Importable as ``import_support`` from both pytest and ``unittest discover`` (no
package-relative imports, no pytest fixtures), like ``support.py``.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from fitbit_monitor.api.auth import CredentialStore, TokenSet
from fitbit_monitor.api.catalog import ALL_SCOPES
from fitbit_monitor.api.client import FitbitClient
from fitbit_monitor.api.http import ReplayTransport, Response
from fitbit_monitor.config import ApiSettings, ImportSettings
from fitbit_monitor.ingest.pipeline import Ingest
from fitbit_monitor.ingest.runlog import RunLog
from fitbit_monitor.store.watermarks import Watermarks
from fitbit_monitor.store.writer import SeriesStore

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "api"

#: The window the recorded fixtures cover.
SINCE = "2026-05-01"
UNTIL = "2026-05-14"

#: The four ``googlehealth.*`` scopes a working setup grants (ADR-0002 §4). Imported
#: rather than restated, so a scope rename cannot leave the fixtures claiming something
#: the catalog no longer asks for.
ALL_SCOPES = tuple(ALL_SCOPES)

#: What the recorded fixtures are expected to land (asserted in the e2e test).
#:
#: The counts are the corpus's, not the API's: ``heart_rate`` is 13 days of the 5-minute
#: recording cadence, and the two days missing from ``heart_rate`` and ``spo2`` are absent
#: from the answer rather than answered with a 404 - which is how v4 reports a period with
#: nothing in it (ADR-0002 §6).
EXPECTED_ROWS = {
    "heart_rate": 3744,  # 13 days x 288 buckets (no data on 2026-05-05)
    "daily_summary": 14,  # one row a day, merged from five data types
    "hrv": 14,
    "sleep": 16,  # 14 nights plus 2 naps
    "spo2": 13,  # no data on 2026-05-09
}


def write_credentials(path: Path, *, scopes: Iterable[str] = ALL_SCOPES, expires_in: float = 8 * 3600) -> TokenSet:
    tokens = TokenSet(
        access_token="recorded-access-token",
        refresh_token="recorded-refresh-token",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        scopes=tuple(scopes),
        user_id="42",
    )
    CredentialStore(path).save(tokens)
    return tokens


def replay_transport(extra: dict[str, dict] | None = None, *, drop: tuple[str, ...] = ()) -> ReplayTransport:
    """The recorded fixtures, optionally patched.

    ``drop`` names exact fixture keys to remove ("pretend that day was never recorded").
    ``extra`` adds or replaces records; a key with no ``?`` names an *endpoint* rather
    than a request, and replaces every recorded answer for it - which is what a test
    wants when it says "the SpO2 data type answers 403", since v4 addresses a period in
    the query, not in the path.
    """
    from fitbit_monitor.api.http import load_fixtures

    fixtures = load_fixtures(FIXTURES)
    for key in drop:
        fixtures.pop(key, None)
    extra = extra or {}
    for key, value in extra.items():
        if "?" in key:
            continue
        for recorded in [k for k in fixtures if k.split("?", 1)[0] == key]:
            del fixtures[recorded]
    fixtures.update(extra)
    return ReplayTransport(fixtures)


def make_client(
    directory: Path,
    *,
    transport=None,
    scopes: Iterable[str] = ALL_SCOPES,
    settings: ImportSettings | None = None,
    api: ApiSettings | None = None,
    sleep=None,
) -> FitbitClient:
    credentials = CredentialStore(directory / "credentials.json")
    if not credentials.exists:
        write_credentials(directory / "credentials.json", scopes=scopes)
    return FitbitClient(
        api or ApiSettings(),
        credentials,
        import_settings=settings or ImportSettings(),
        transport=transport or replay_transport(),
        sleep=sleep or (lambda _seconds: None),
        etag_cache_path=directory / "etag-cache.json",
    )


class Environment:
    """A scratch store + client + ingest + watermarks + runlog, as one object."""

    def __init__(self, *, transport=None, scopes=ALL_SCOPES, settings=None, api=None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = SeriesStore(self.dir / "store.sqlite3")
        self.ingest = Ingest(self.store, source="api")
        self.watermarks = Watermarks(self.store)
        self.runlog = RunLog(self.store)
        self.client = make_client(self.dir, transport=transport, scopes=scopes, settings=settings, api=api)
        self.settings = settings or ImportSettings()

    def cleanup(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def counts(self) -> dict[str, int]:
        return {dataset: self.store.count(dataset) for dataset in self.store.datasets()}


class CollectingProgress:
    """The importer's progress sink, recorded for assertions."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.chunks: list[tuple[str, int, int, str]] = []
        self.finished: list[tuple[str, str]] = []
        self.notes: list[str] = []

    def dataset_started(self, plan) -> None:
        self.started.append(plan.dataset)

    def chunk_done(self, dataset, index, total, label, stats, remaining) -> None:
        self.chunks.append((dataset, index, total, label))
        self.remaining = remaining

    def note(self, text: str) -> None:
        self.notes.append(text)

    def dataset_finished(self, report) -> None:
        self.finished.append((report.dataset, report.status))


class ScriptedTransport:
    """Answers from a mapping of ``path substring -> Response``, defaulting to 404."""

    def __init__(self, routes: dict[str, tuple[int, object]], *, default: tuple[int, object] = (404, {"errors": []}), raise_on=None):
        self.routes = routes
        self.default = default
        self.raise_on = raise_on or (lambda path, count: False)
        self.calls: list[str] = []
        self.rate_remaining = 100

    def request(self, method, url, headers=None, body=None) -> Response:
        path = url.split("?")[0]
        self.calls.append(path)
        self.rate_remaining -= 1
        if self.raise_on(path, self.calls.count(path)):
            raise self.raise_on(path, self.calls.count(path))
        for needle, (status, payload) in self.routes.items():
            if needle in path:
                import json

                return Response(status, json.dumps(payload).encode(), {"X-RateLimit-Remaining": str(self.rate_remaining)})
        import json

        return Response(self.default[0], json.dumps(self.default[1]).encode(), {"X-RateLimit-Remaining": str(self.rate_remaining)})


def temp_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory()
