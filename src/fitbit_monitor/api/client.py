"""The polite Google Health API client: auth, retries, a local rate ceiling, ETags.

One class, two verbs (:meth:`HealthClient.get` for a ``list``, :meth:`HealthClient.post`
for a ``rollUp``), and a hard rule that nothing here is a surprise to the caller:

* **Pace yourself, because the API will not tell you to.** The Google Health API allows
  300 requests/minute/user and publishes **no** ``X-RateLimit-*`` headers to pace
  against, so the legacy client's "read the ceiling off the response and stop before
  it" scheme has nothing to read. :class:`TokenBucket` is the replacement: a local
  ceiling (``[api] requests_per_second``, 5/s by default) that spends a request
  *before* it is made instead of discovering afterwards that it was one too many.
* **Retries with jittered backoff** on ``429 RESOURCE_EXHAUSTED``, ``503 UNAVAILABLE``
  and ``504 DEADLINE_EXCEEDED`` - the trio Google's own rate-limits page names -
  honouring ``Retry-After`` when there is one. Any other 4xx is a real answer and is
  raised, not retried.
* **Tokens are refreshed before they expire**, never after a request fails with 401 if
  it can be helped - and if the refresh itself is refused, the message says
  ``fm auth login``.
* **ETags** are cached on disk, so re-importing an overlapping window costs 304s
  instead of full payloads. Only GETs: a ``rollUp`` POST is not conditional.
* **A retired host is refused, not dialled.** ``[api] base_url`` pointing at the Fitbit
  Web API - switched off on 2026-11-02, still answering ``401`` - raises
  :class:`~fitbit_monitor.api.errors.SourceRetiredError` before the request rather than
  returning an answer that reads like a bad login. See :mod:`fitbit_monitor.api.source`.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import ApiSettings, ImportSettings
from ..timeutil import utcnow
from . import source as source_state
from .auth import CredentialStore, TokenSet, refresh
from .catalog import short_scope
from .errors import (
    MISSING_SCOPE_REASONS,
    CredentialsExpiredError,
    HTTPError,
    NotAuthenticatedError,
    RateLimitError,
    SourceRetiredError,
)
from .http import Response, UrllibTransport

log = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 0.5
#: The statuses the API's own documentation says to retry, with exponential backoff and
#: random jitter: rate limited, and the two transient unavailability codes.
RETRY_STATUSES = (429, 503, 504)
#: The AIP reason strings that mean "you asked without a scope that covers it" are named
#: in :mod:`.errors`, next to the ``missing_scope`` property that reads them.
#: The answer that means "the Google account is not linked to a Fitbit account", which
#: is a first-run fact about the account and not a fault in the request (ADR-0002 §7).
ACCOUNT_NOT_LINKED = "ACCOUNT_NOT_LINKED"


@dataclass
class RateLimit:
    """The last rate-limit reading the API gave us, in wall-clock terms.

    Kept because it is still *useful* when a response carries one (a proxy or an older
    gateway may), but it is now an additional signal rather than the mechanism: the
    v4 API sends none, and a client that only paces itself off headers a service does
    not send will discover its ceiling by hitting it.
    """

    limit: int | None = None
    remaining: int | None = None
    reset_in: float | None = None
    scope: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_in": round(self.reset_in, 1) if self.reset_in is not None else None,
            "scope": self.scope,
        }


class TokenBucket:
    """A local ceiling of ``rate`` requests per second, with a one-second burst.

    Deliberately tiny and self-contained: it owns a clock and a sleep function so an
    import of ten thousand requests can be tested in milliseconds by handing it both.
    A bucket - rather than "sleep 200ms between requests" - is what lets a chunk that
    waited for a retry catch up without going over the minute-long window the API
    actually measures.
    """

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        capacity: float | None = None,
    ) -> None:
        self.rate = max(0.1, float(rate))
        self.capacity = float(capacity) if capacity else max(1.0, self.rate)
        self.clock = clock
        self.sleep = sleep
        self.tokens = self.capacity
        self.updated = self.clock()
        self.waited = 0.0
        self.waits = 0

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self.updated)
        self.updated = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    def take(self, cost: float = 1.0) -> float:
        """Spend one token, waiting if the bucket is empty. Returns the seconds slept."""
        self._refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return 0.0
        deficit = cost - self.tokens
        pause = deficit / self.rate
        self.sleep(pause)
        self.waited += pause
        self.waits += 1
        self.tokens = 0.0
        self.updated = self.clock()
        return pause


class ETagCache:
    """Tiny on-disk ``path -> (etag, body)`` map. Missing or broken = empty."""

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        self._entries: dict[str, dict[str, str]] = {}
        self.writes = 0
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                loaded = {}
            if isinstance(loaded, dict):
                self._entries = {k: dict(v) for k, v in loaded.items() if isinstance(v, dict)}

    def etag_for(self, key: str) -> str | None:
        entry = self._entries.get(key)
        return entry.get("etag") if entry else None

    def body_for(self, key: str) -> bytes | None:
        entry = self._entries.get(key)
        if not entry or "body" not in entry:
            return None
        return str(entry["body"]).encode("utf-8")

    def remember(self, key: str, etag: str, body: bytes) -> None:
        self._entries[key] = {"etag": etag, "body": body.decode("utf-8", errors="replace")}
        self.writes += 1

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def __len__(self) -> int:
        return len(self._entries)


class HealthClient:
    """Everything that talks to ``health.googleapis.com`` goes through here."""

    def __init__(
        self,
        api: ApiSettings | None = None,
        credentials: CredentialStore | None = None,
        *,
        import_settings: ImportSettings | None = None,
        transport: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], Any] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        etag_cache_path: Path | None = None,
    ) -> None:
        self.api = api or ApiSettings()
        self.credentials = credentials if credentials is not None else CredentialStore(
            Path(self.api.credentials_path or Path.home() / ".local" / "share" / "fitbit-monitor" / "credentials.json")
        )
        self.settings = import_settings or ImportSettings()
        self.transport = transport if transport is not None else UrllibTransport(timeout=self.api.timeout)
        self.sleep = sleep
        self.clock = clock
        #: Only used for backoff jitter, so that a test can pin it and a run cannot.
        self.rng = rng or random.Random()
        self.etags = ETagCache(etag_cache_path)
        self.rate_limit = RateLimit()
        #: Whether this client's transport can reach a real host (see :mod:`.http`), and
        #: therefore what the retired-source guard in :mod:`.source` binds.
        self.live = bool(getattr(self.transport, "live", False))
        # A fixture replay is not a service to be polite to: the bucket exists to keep a
        # real host from seeing a burst, and applying it to a ReplayTransport would only
        # make the test suite slow. Same ``live`` rule as the retired-source guard, so a
        # stand-in transport is treated as a stand-in in both places.
        self.bucket = TokenBucket(
            self.api.requests_per_second if self.live else float("inf"), clock=monotonic, sleep=sleep
        )
        self.requests = 0
        self.not_modified = 0
        # Computed once: an import makes thousands of requests against the same few
        # endpoints, and re-parsing them per request would be pure overhead on the hot path.
        self._guard_state = source_state.state_of(self.api, settings=source_state.DATA_PLANE_SETTINGS)
        self._tokens: TokenSet | None = self.credentials.load_or_none()

    def guard_source(self, *, extra: Sequence[str] = ()) -> None:
        """Refuse to dial a host that was switched off. Raises :class:`SourceRetiredError`.

        Called before a request goes out, never in the constructor: a client may exist
        purely to read state (``fm auth status``, ``fm doctor``) and those commands should
        report the retirement, not fail with it. Only a live transport is guarded, so
        ``--fixtures`` replay and the test suite keep working.

        ``extra`` names a URL that is not in ``[api]`` and is about to be dialled (a token
        endpoint handed in by an argument). The ``[api]`` data-plane endpoints - the API
        and the token endpoint, which is all a client that already holds a token dials -
        are checked from the state computed at construction.
        """
        state = self._guard_state
        if not self.live:
            return
        retired = state.retired + tuple(
            ("endpoint", url) for url in extra if source_state.is_retired_host(url)
        )
        if retired:
            raise SourceRetiredError(
                source_state.SourceState(source_state.STATE_RETIRED, state.endpoints, tuple(retired)).reason()
            )

    def source_state(self) -> source_state.SourceState:
        """What ``[api]`` points at, including the consent page: the whole configuration."""
        return source_state.state_of(self.api)

    # --- credentials --------------------------------------------------------
    @property
    def tokens(self) -> TokenSet:
        if self._tokens is None:
            self._tokens = self.credentials.load_or_none()
        if self._tokens is None:
            raise NotAuthenticatedError()
        return self._tokens

    @property
    def authenticated(self) -> bool:
        return self._tokens is not None or self.credentials.exists

    def set_tokens(self, tokens: TokenSet) -> None:
        self._tokens = tokens

    def ensure_fresh(self, *, force: bool = False) -> TokenSet:
        """Refresh the access token if it is expired or nearly so.

        The refresh is logged - name and outcome only, never a token - because a refresh
        that fails quietly is the failure mode an import cannot recover from on its own,
        and the log is the only place a user will look. See
        :mod:`fitbit_monitor.logsecrets` for why a log line here cannot leak a credential.
        """
        tokens = self.tokens
        if not force and not tokens.needs_refresh(self.clock()):
            log.debug("access token still valid (expires %s)", tokens.expires_at.isoformat(timespec="seconds"))
            return tokens
        # A retired token endpoint cannot issue a token, and saying "re-consent" about a
        # service that issues nothing is the loop this guard exists to break.
        self.guard_source()
        log.debug("refreshing the access token (%s)", "API said 401" if force else "expired or nearly so")
        try:
            refreshed = refresh(
                self.transport,
                token_url=self.api.token_url,
                client_id=self.api.client_id,
                client_secret=self.api.client_secret,
                refresh_token=tokens.refresh_token,
                now=self.clock(),
            )
        except CredentialsExpiredError as exc:
            log.error("token refresh refused: %s", exc)
            raise
        self.credentials.save(refreshed)
        self._tokens = refreshed
        log.info(
            "access token refreshed (scopes: %s); valid until %s",
            ", ".join(short_scope(s) for s in refreshed.scopes) or "none reported",
            refreshed.expires_at.isoformat(timespec="seconds"),
        )
        return refreshed

    def post_form(self, url: str, fields: Mapping[str, str], *, headers: Mapping[str, str] | None = None) -> Response:
        """Form POST, used for the token endpoints (which are not versioned APIs)."""
        self.guard_source(extra=(url,))
        body = urllib.parse.urlencode(dict(fields)).encode("ascii")
        merged = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        merged.update(dict(headers or {}))
        self.requests += 1
        return self.transport.request("POST", url, merged, body)

    # --- the verb everything else uses --------------------------------------
    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Response:
        """One API call, with the politeness rules applied.

        ``GET`` carries its window in the query string and may be answered from the ETag
        cache; ``POST`` - the ``rollUp`` and ``dailyRollUp`` verbs - carries it in a JSON
        body and is never conditional, because an ETag on an aggregation request would
        be asserting that a window's contents are immutable.
        """
        verb = method.upper()
        url = self._url(path, params)
        self.guard_source()
        payload: bytes | None = None
        content_type = "application/json"
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        # A stable digest, not ``hash()``: the ETag cache is a file, and Python's salted
        # hashes would give every run a different cache full of entries it can never use.
        cache_key = url if verb == "GET" else f"{verb} {url}:{sha1(payload or b'').hexdigest()[:16]}"
        attempts = max(1, int(self.api.retries) + 1)
        refreshed = False
        if self._tokens is None and not self.credentials.exists:
            # Better to say "log in" than to dial the API with no credential and report
            # the 401 that follows as an API failure.
            raise NotAuthenticatedError()
        if self._tokens is not None and self._can_refresh() and self.tokens.needs_refresh(self.clock()):
            # Refresh on the schedule, not after a 401: a doomed request is still a request.
            self.ensure_fresh()
        for attempt in range(attempts):
            self._pace()
            headers = dict(self.tokens.authorization_header())
            headers["Accept"] = "application/json"
            if payload is not None:
                headers["Content-Type"] = content_type
            etag = self.etags.etag_for(cache_key) if verb == "GET" else None
            if etag:
                headers["If-None-Match"] = etag
            response = self.transport.request(verb, url, headers, payload)
            self.requests += 1
            self._note_rate_limit(response)

            if response.not_modified:
                cached = self.etags.body_for(cache_key)
                if cached is not None:
                    self.not_modified += 1
                    log.debug("304 %s", path)
                    return Response(200, cached, response.headers)
                response = Response(404, b'{"error":{"message":"ETag cache lost the body"}}', response.headers)

            if response.status == 401 and not refreshed and self._can_refresh():
                self.ensure_fresh(force=True)
                refreshed = True
                continue

            if response.status in RETRY_STATUSES:
                if attempt + 1 >= attempts:
                    if response.status == 429:
                        raise RateLimitError(
                            f"rate limited on {path}",
                            reset_in=_reset_in(response),
                        )
                    raise HTTPError(
                        response.status,
                        f"giving up on {path} after {attempts} attempts",
                        path=path,
                        detail=_error_detail(response),
                    )
                self.sleep(_backoff(attempt, response, self.rng))
                continue

            if not response.ok:
                raise HTTPError(
                    response.status,
                    f"the API refused {path}",
                    path=path,
                    detail=_error_detail(response),
                    reason=_error_reason(response),
                )

            if etag_header := response.header("ETag"):
                self.etags.remember(cache_key, etag_header, response.body)
            return response
        raise HTTPError(500, f"no response from {path}", path=path)  # pragma: no cover - loop always returns

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Response:
        """GET ``path`` (relative to ``[api] base_url``)."""
        return self.request("GET", path, params)

    def post(self, path: str, body: Mapping[str, Any] | None = None) -> Response:
        """POST a JSON body - the only way to ask for a rollup."""
        return self.request("POST", path, None, body)

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.get(path, params).json()

    def post_json(self, path: str, body: Mapping[str, Any] | None = None) -> Any:
        return self.post(path, body).json()

    def profile(self) -> dict:
        """``users/me/profile`` - who this account is, and since when.

        The legacy client used ``/1/user/-/profile.json`` for the same two jobs: a probe
        that a token works, and ``memberSinceDate`` for ``--full-history``. v4 keeps both
        jobs and loses one thing: there is no ``resources`` list, so a scope grant is the
        only evidence of what an account has (see
        :func:`fitbit_monitor.api.catalog.permitted_datasets`).
        """
        payload = self.get_json("users/me/profile")
        return dict(payload) if isinstance(payload, dict) else {}

    def identity(self) -> dict:
        """``users/me/identity`` - the ids this token is bound to.

        Any health scope is enough for this call, which is why ``fm auth status`` prints
        it rather than the profile: profile needs ``profile.readonly``, and a user who
        declined it is still a user whose identity is worth showing - especially when the
        failure is ``client_user_mismatch`` on a Personal-mode project.
        """
        payload = self.get_json("users/me/identity")
        return dict(payload) if isinstance(payload, dict) else {}

    def flush(self) -> None:
        """Persist what this run learned (the ETag cache). Idempotent."""
        self.etags.save()

    # --- internals ----------------------------------------------------------
    def _url(self, path: str, params: Mapping[str, Any] | None) -> str:
        clean = path if path.startswith("/") else f"/{path}"
        user = (self.api.user_id or "me").strip("/")
        if user != "me":
            # One swap, at the front of the path: everything in the catalog is written
            # against `users/me/...` because that is what a single-account tool wants.
            clean = clean.replace("/users/me/", f"/users/{user}/", 1)
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, quote_via=urllib.parse.quote
            )
        return f"{self.api.base_url}{clean}{query}"

    def _can_refresh(self) -> bool:
        tokens = self._tokens
        return bool(tokens and tokens.refresh_token and self.api.client_id)

    def _note_rate_limit(self, response: Response) -> None:
        limit = _int_header(response, "X-RateLimit-Limit")
        remaining = _int_header(response, "X-RateLimit-Remaining")
        reset = _reset_in(response)
        interval = response.header("X-RateLimit-Limit-Interval")
        if limit is None and remaining is None and reset is None:
            return
        self.rate_limit = RateLimit(
            limit=limit if limit is not None else self.rate_limit.limit,
            remaining=remaining if remaining is not None else self.rate_limit.remaining,
            reset_in=reset if reset is not None else self.rate_limit.reset_in,
            scope=interval or self.rate_limit.scope,
        )

    def _pace(self) -> None:
        """Spend a token from the local bucket, then honour a ceiling the API did state."""
        self.bucket.take()
        self._respect_rate_limit()

    def _respect_rate_limit(self) -> None:
        """Stop *before* a stated ceiling and wait for the reset, within the run's patience.

        The v4 API sends no such ceiling, so on the normal path this does nothing and the
        local bucket is what keeps the import polite. It stays because a response that
        *does* carry ``X-RateLimit-*`` - a proxy, a gateway, a future revision - is worth
        obeying, and because ``[import] rate_limit_reserve`` and ``max_wait_seconds`` are
        documented knobs that a user is entitled to have mean something.
        """
        reading = self.rate_limit
        if reading.remaining is None:
            return
        if reading.remaining > self.settings.rate_limit_reserve:
            return
        wait = reading.reset_in or _RESET_FALLBACK_SECONDS
        if wait > self.settings.max_wait_seconds:
            raise RateLimitError(
                f"rate limit exhausted with {int(wait)}s until reset",
                reset_in=wait,
            )
        log.info("rate limit: %.0fs wait for reset (%d left)", wait, reading.remaining)
        self.sleep(wait)
        self.rate_limit = RateLimit(
            limit=reading.limit, remaining=self.settings.rate_limit_reserve + 1, reset_in=None, scope=reading.scope
        )


_RESET_FALLBACK_SECONDS = 60.0


def _int_header(response: Response, name: str) -> int | None:
    raw = response.header(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _reset_in(response: Response) -> float | None:
    """How long to wait, from whichever of the two headers exists. v4 sends neither,
    and ``Retry-After`` is the one a Google front door does use."""
    retry_after = response.header("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            return None
    seconds = _int_header(response, "X-RateLimit-Reset")
    return float(seconds) if seconds is not None else None


def _backoff(attempt: int, response: Response, rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter, or the server's own number if it gave one.

    The jitter is the point: a hundred clients that all sleep ``2**attempt`` after the
    same 503 retry in lockstep and produce the next one.
    """
    stated = _reset_in(response)
    if stated:
        return min(60.0, max(1.0, stated))
    ceiling = min(60.0, BACKOFF_BASE_SECONDS * (2**attempt))
    return (rng or random).uniform(0.0, ceiling)


def _error_envelope(response: Response) -> dict[str, Any]:
    """The AIP error body, or ``{}``. Best-effort by construction: an error body that
    does not parse must not become a second, more interesting error."""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - error bodies are best-effort
        return {}
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    return dict(error) if isinstance(error, dict) else {}


def _error_detail(response: Response) -> str | None:
    """``UNAUTHENTICATED: Request is missing required authentication credential``.

    The v4 dialect is one AIP-193 envelope - ``error.status``, ``error.message``, and
    ``error.details[]`` carrying a ``reason`` - where the legacy API used its own
    ``errors[]`` array with ``errorType``. Reading only the legacy shape is how a real
    reason gets thrown away: the two are tried in that order, so a body in either
    dialect still arrives at the CLI with its reason attached.
    """
    error = _error_envelope(response)
    if error:
        status = str(error.get("status") or "")
        message = str(error.get("message") or "")
        text = f"{status}: {message}".strip(": ")
        return text[:200] or None
    if isinstance(payload := _legacy_payload(response), list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            parts = [str(first.get("message") or first.get("errorType") or first), str(first.get("location") or "")]
            return " ".join(p for p in parts if p).strip()[:200]
        return str(first)[:200]
    return response.text[:200] or None


def _error_reason(response: Response) -> str | None:
    """The machine-readable reason, for callers that branch on it.

    ``MISSING_OAUTH_SCOPE`` and ``ACCOUNT_NOT_LINKED`` are different conversations with
    the user than the status code is, and a message truncated for a terminal is not a
    thing to branch on.
    """
    for detail in _error_envelope(response).get("details") or []:
        if isinstance(detail, dict):
            reason = detail.get("reason") or (detail.get("metadata") or {}).get("reason")
            if reason:
                return str(reason)
    return None


def _legacy_payload(response: Response) -> Any:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return None
    return payload.get("errors") if isinstance(payload, dict) else None


#: The legacy name still works, and is still what old scripts import. Renaming the client
#: is honest - it does not talk to Fitbit - but breaking ``from fitbit_monitor.api.client
#: import FitbitClient`` in a user's plugin for a name is not, so the alias stays and the
#: same argument ADR-0002 §9 makes for ``--source api`` applies here.
FitbitClient = HealthClient


__all__ = [
    "ACCOUNT_NOT_LINKED",
    "ETagCache",
    "FitbitClient",
    "HealthClient",
    "MISSING_SCOPE_REASONS",
    "RateLimit",
    "RETRY_STATUSES",
    "TokenBucket",
]
