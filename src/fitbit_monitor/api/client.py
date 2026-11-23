"""The polite Fitbit API client: auth, retries, rate-limit budget, ETags.

One class, one public verb (:meth:`FitbitClient.get`), and a hard rule that nothing
here is a surprise to the caller:

* **Rate limit first.** Every response carries ``X-RateLimit-Remaining`` and
  ``-Reset``; the client keeps the last reading, stops *before* the ceiling
  (``[import] rate_limit_reserve``) and waits for the reset rather than collecting
  429s. ``[import] max_wait_seconds`` bounds how long one run will sit still - past
  that it raises :class:`~fitbit_monitor.api.errors.RateLimitError` and the caller
  re-runs later, because watermarks make re-running cheap.
* **Retries with backoff** on 429 and 5xx, honouring ``Retry-After``. 4xx that is
  not 401/429 is a real answer (no data for that day) and is raised, not retried.
* **Tokens are refreshed before they expire**, never after a request fails with 401
  if it can be helped - and if the refresh itself is refused, the message says
  ``fm auth login``.
* **ETags** are cached on disk, so re-importing an overlapping window costs 304s
  instead of full payloads.
* **A retired host is refused, not dialled.** ``[api] base_url`` pointing at the Fitbit
  Web API - switched off on 2026-11-02, still answering ``401`` - raises
  :class:`~fitbit_monitor.api.errors.SourceRetiredError` before the request rather than
  returning an answer that reads like a bad login. See :mod:`fitbit_monitor.api.source`.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import ApiSettings, ImportSettings
from ..timeutil import utcnow
from . import source as source_state
from .auth import CredentialStore, TokenSet, refresh
from .errors import (
    CredentialsExpiredError,
    HTTPError,
    NotAuthenticatedError,
    RateLimitError,
    SourceRetiredError,
)
from .http import Response, UrllibTransport

log = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 0.5


@dataclass
class RateLimit:
    """The last rate-limit reading the API gave us, in wall-clock terms."""

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


class FitbitClient:
    """Everything that talks to ``api.fitbit.com`` goes through here."""

    def __init__(
        self,
        api: ApiSettings | None = None,
        credentials: CredentialStore | None = None,
        *,
        import_settings: ImportSettings | None = None,
        transport: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], Any] = utcnow,
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
        self.etags = ETagCache(etag_cache_path)
        self.rate_limit = RateLimit()
        self.requests = 0
        self.not_modified = 0
        #: Whether this client's transport can reach a real host (see :mod:`.http`), and
        #: therefore what the retired-source guard in :mod:`.source` binds.
        self.live = bool(getattr(self.transport, "live", False))
        # Computed once: an import makes thousands of requests against the same three
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
            ", ".join(refreshed.scopes) or "none reported",
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
    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Response:
        """GET ``path`` (relative to ``[api] base_url``), with the politeness rules applied."""
        url = self._url(path, params)
        self.guard_source()
        cache_key = url
        attempts = max(1, int(self.api.retries) + 1)
        refreshed = False
        if self._tokens is not None and self._can_refresh() and self.tokens.needs_refresh(self.clock()):
            # Refresh on the schedule, not after a 401: a doomed request is still a request.
            self.ensure_fresh()
        for attempt in range(attempts):
            self._respect_rate_limit()
            headers = dict(self.tokens.authorization_header())
            headers["Accept"] = "application/json"
            etag = self.etags.etag_for(cache_key)
            if etag:
                headers["If-None-Match"] = etag
            response = self.transport.request("GET", url, headers, None)
            self.requests += 1
            self._note_rate_limit(response)

            if response.not_modified:
                cached = self.etags.body_for(cache_key)
                if cached is not None:
                    self.not_modified += 1
                    log.debug("304 %s", path)
                    return Response(200, cached, response.headers)
                response = Response(404, b'{"errors":[{"message":"ETag cache lost the body"}]}', response.headers)

            if response.status == 401 and not refreshed and self._can_refresh():
                self.ensure_fresh(force=True)
                refreshed = True
                continue

            if response.status == 429 or response.status >= 500:
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
                self.sleep(_backoff(attempt, response))
                continue

            if not response.ok:
                raise HTTPError(response.status, f"the API refused {path}", path=path, detail=_error_detail(response))

            if etag_header := response.header("ETag"):
                self.etags.remember(cache_key, etag_header, response.body)
            return response
        raise HTTPError(500, f"no response from {path}", path=path)  # pragma: no cover - loop always returns

    def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.get(path, params).json()

    def profile(self) -> dict:
        """The profile call doubles as "who am I, and what may this token see"."""
        payload = self.get_json("/1/user/-/profile.json")
        return dict(payload) if isinstance(payload, dict) else {}

    def flush(self) -> None:
        """Persist what this run learned (the ETag cache). Idempotent."""
        self.etags.save()

    # --- internals ----------------------------------------------------------
    def _url(self, path: str, params: Mapping[str, Any] | None) -> str:
        clean = path if path.startswith("/") else f"/{path}"
        if self.api.user_id and self.api.user_id != "-":
            clean = clean.replace("/user/-/", f"/user/{self.api.user_id}/", 1)
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
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

    def _respect_rate_limit(self) -> None:
        """Stop *before* the ceiling and wait for the reset, within the run's patience."""
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
    seconds = _int_header(response, "X-RateLimit-Reset")
    if seconds is not None:
        return float(seconds)
    retry_after = response.header("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            return None
    return None


def _backoff(attempt: int, response: Response) -> float:
    stated = _reset_in(response)
    if stated:
        return min(60.0, max(1.0, stated))
    return BACKOFF_BASE_SECONDS * (2**attempt)


def _error_detail(response: Response) -> str | None:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - error bodies are best-effort
        return response.text[:200] or None
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                parts = [str(first.get("message") or first.get("errorType") or first), str(first.get("location") or "")]
                return " ".join(p for p in parts if p).strip()[:200]
            return str(first)[:200]
        if payload:
            return json.dumps(payload)[:200]
    return None


__all__ = ["ETagCache", "FitbitClient", "RateLimit"]
