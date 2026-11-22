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
)
from .http import RecordingTransport, ReplayTransport, Response, UrllibTransport

__all__ = [
    "ALL_SCOPES",
    "CATALOG",
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
    "TokenSet",
    "UrllibTransport",
    "exchange_code",
    "lookup",
    "permitted_datasets",
    "pkce_challenge",
    "refresh",
    "resolve_names",
]
