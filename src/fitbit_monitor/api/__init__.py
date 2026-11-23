"""The health-API source: OAuth, a polite HTTP client, and the dataset catalog.

Three pieces, each usable on its own and each deliberately replaceable:

* :mod:`fitbit_monitor.api.auth` - OAuth2 + PKCE with a loopback redirect, and the
  ``0600`` token file (``CredentialStore``).
* :mod:`fitbit_monitor.api.client` - :class:`~fitbit_monitor.api.client.FitbitClient`:
  bearer auth and refresh, retries with backoff, the rate-limit budget, on-disk ETags.
* :mod:`fitbit_monitor.api.catalog` - what to ask for: one :class:`~fitbit_monitor.api.catalog.Dataset`
  per importable dataset, with its scope, chunk size, requests and normalizer.

The transports in :mod:`fitbit_monitor.api.http` are what keeps all of this testable:
:class:`~fitbit_monitor.api.http.ReplayTransport` answers from recorded fixtures and
refuses anything unrecorded, so the whole import path runs offline in CI with no
secrets and no internet.

:mod:`fitbit_monitor.api.source` is the one that knows *which* upstream host that is, that
the legacy Fitbit Web API was switched off on 2026-11-02, and that dialling it anyway
only produces a ``401`` that looks like a bad login. It refuses retired endpoints before
they are dialled and answers "is anything there?" when asked.
"""

from __future__ import annotations

from .auth import CredentialStore, PkceFlow, TokenSet, exchange_code, pkce_challenge, refresh
from .catalog import ALL_SCOPES, CATALOG, Dataset, Request, lookup, permitted_datasets, resolve_names
from .client import ETagCache, FitbitClient, RateLimit
from .errors import (
    APIUnavailableError,
    CredentialsExpiredError,
    HTTPError,
    NotAuthenticatedError,
    RateLimitError,
    ReplayMissError,
    SourceRetiredError,
)
from .http import RecordingTransport, ReplayTransport, Response, UrllibTransport
from .source import (
    DECIDED_BASE_URL,
    DECISION_DOC,
    LEGACY_SUNSET,
    SourceState,
    check_alive,
    guard,
    is_retired_host,
    state_of,
)

__all__ = [
    "ALL_SCOPES",
    "CATALOG",
    "DECIDED_BASE_URL",
    "DECISION_DOC",
    "LEGACY_SUNSET",
    "APIUnavailableError",
    "CredentialStore",
    "CredentialsExpiredError",
    "Dataset",
    "ETagCache",
    "FitbitClient",
    "HTTPError",
    "NotAuthenticatedError",
    "PkceFlow",
    "RateLimit",
    "RateLimitError",
    "RecordingTransport",
    "ReplayMissError",
    "ReplayTransport",
    "Request",
    "Response",
    "SourceRetiredError",
    "SourceState",
    "TokenSet",
    "UrllibTransport",
    "check_alive",
    "exchange_code",
    "guard",
    "is_retired_host",
    "lookup",
    "permitted_datasets",
    "pkce_challenge",
    "refresh",
    "resolve_names",
    "state_of",
]
