"""Errors raised while talking to the health API.

They all derive from :class:`FmError`, so the CLI's single ``except`` boundary keeps
working and every command still maps a failure to an exit code. The split that matters
to a user is three-way, not two-way:

* **fix me now** - authentication: ``fm auth login`` will actually help
  (:class:`NotAuthenticatedError`, :class:`CredentialsExpiredError`);
* **wait, then re-run** - a rate limit or a 5xx, where watermarks make the work already
  done stick (:class:`RateLimitError`, :class:`APIUnavailableError`);
* **no action helps** - the source itself is gone
  (:class:`SourceRetiredError`). This one must never be confused with the first: the
  retired Fitbit host answers ``401``, and telling somebody to log in against a service
  that issues no tokens is a loop they can never leave.
"""

from __future__ import annotations

from ..errors import FmError, UsageError

#: Named inline rather than imported from :mod:`fitbit_monitor.api.source`, which imports
#: these errors - the other direction would be a cycle.
DECISION_DOC = "docs/adr/0002-health-api-source.md"
DECIDED_TARGET = "the Google Health API (https://health.googleapis.com/v4)"

#: AIP reasons that mean "this token cannot see that", kept here rather than in
#: :mod:`fitbit_monitor.api.client` so both the client and the importer can name a scope
#: refusal without either importing the other's internals.
MISSING_SCOPE_REASONS = frozenset({"MISSING_OAUTH_SCOPE", "PERMISSION_DENIED"})


class FitbitAPIError(FmError):
    """Base class for anything that went wrong on the way to or from the API."""

    exit_code = 1


class SourceRetiredError(FitbitAPIError):
    """The configured source no longer exists upstream, so nothing this command does can work.

    Raised *before* the request rather than after the answer, and with its own exit code
    (**6**) so a script can tell "this endpoint is retired" from "my token needs
    attention" - the two look identical in the API's own reply. See
    :mod:`fitbit_monitor.api.source` for what is retired and
    ``docs/adr/0002-health-api-source.md`` §10 for the port.
    """

    exit_code = 6  # the CLI's EXIT_SOURCE (fitbit_monitor.cli.common), kept as a literal
    # here so the API package does not import from the CLI to name its own exit code.

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint=hint
            or (
                "there is no login that fixes this: the host issues no tokens any more. "
                f"Delete those [api] settings to get the defaults, which are {DECIDED_TARGET}. "
                f"Or skip the API: `fm import --source csv --path ARCHIVE` needs no API at all. "
                f"`fm api source --verify` shows what still answers; {DECISION_DOC} is the "
                "decision and its section 10 the record of the port."
            ),
        )


class NotAuthenticatedError(FitbitAPIError):
    """There are no stored credentials to make the call with."""

    exit_code = 2

    def __init__(self, message: str = "not authenticated: no stored credentials", *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint or "run `fm auth login` first")


class CredentialsExpiredError(FitbitAPIError):
    """The refresh token was refused - only the user can fix this one."""

    exit_code = 2

    def __init__(self, message: str = "the stored credentials were rejected") -> None:
        super().__init__(message, hint="run `fm auth login` again to re-consent")


class ConfigMissingError(UsageError):
    """The API is being used before the app was registered in ``config.toml``."""

    exit_code = 2

    def __init__(self, message: str = "no Fitbit app credentials in config.toml") -> None:
        super().__init__(
            message,
            hint="`fm config init`, then set [api] client_id / client_secret and run `fm auth login`",
        )


class HTTPError(FitbitAPIError):
    """The API answered with a status we do not handle internally.

    ``reason`` carries the AIP reason string (``MISSING_OAUTH_SCOPE``,
    ``ACCOUNT_NOT_LINKED``, ``DISALLOWED_OAUTH_SCOPES``) alongside the status. It is
    separate because the two answer different questions: the status says how bad it was,
    the reason says who has to do what next - and "the Google account is not linked to a
    Fitbit account" and "your token lacks the sleep scope" are both a 4xx that would
    otherwise get the same unhelpful message.
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        path: str | None = None,
        detail: str | None = None,
        reason: str | None = None,
    ) -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(f"HTTP {status} from {path or 'the API'}{suffix}" if path else f"HTTP {status}{suffix}")
        self.status = status
        self.path = path
        self.detail = detail
        self.reason = reason

    @property
    def missing_scope(self) -> bool:
        """True when the refusal is "that scope is not on this token".

        This is the answer that makes partial consent survivable: a dataset - or one
        data type's worth of a dataset - that is refused for scope is reported and
        skipped, not treated as an import that failed.
        """
        return self.reason in MISSING_SCOPE_REASONS


class RateLimitError(FitbitAPIError):
    """Rate limited, and waiting was not an option within ``[import] max_wait_seconds``."""

    def __init__(self, message: str = "Fitbit rate limit reached", *, reset_in: float | None = None) -> None:
        wait = "" if reset_in is None else f" (resets in {int(reset_in)}s)"
        super().__init__(
            f"{message}{wait}",
            hint="re-run the same command: chunks already stored are kept and the watermark resumes here",
        )


class APIUnavailableError(FitbitAPIError):
    """Could not reach the API at all (DNS, TLS, connection refused, timeout)."""


class ReplayMissError(FitbitAPIError):
    """A replay fixture was asked for a request that was never recorded."""

    def __init__(self, message: str) -> None:
        super().__init__(message, hint="record it with `python scripts/dev/record_fixtures.py`")
