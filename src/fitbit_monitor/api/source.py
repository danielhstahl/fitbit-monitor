"""Which upstream source this build dials - and whether that host still answers.

ADR-0002 decided the source: the **Google Health API**, ``https://health.googleapis.com/v4``,
because the **legacy Fitbit Web API** is gone - Google turned ``api.fitbit.com`` and the
Fitbit OAuth endpoints off on **2026-11-02** (ADR-0002 §1-§2). The defaults in
:mod:`fitbit_monitor.config` are that decision, so a fresh install dials the live source.

What this module is still for is the config that outlived the service: a ``[api]`` table
pinned at ``api.fitbit.com``, a token endpoint copy-pasted from a 2025 blog post. And a
dead API is the one failure an import tool must never report as an authentication problem,
because from a terminal the two look identical - a ``401`` - and the advice they need is
opposite. One means *go and log in again*; the other means *this endpoint will never issue
a token again, whatever you do*. Measured from this repository on 2026-11-23 with no
credentials at all:

===================================================  =========  ==========================
request                                              status     signal in the body
===================================================  =========  ==========================
``GET  api.fitbit.com/1/user/-/profile.json``        401        ``UNAUTHENTICATED``
``POST api.fitbit.com/oauth2/token``                 401        ``invalid_client``
``GET  health.googleapis.com/v4/users/me/...``       401        ``CREDENTIALS_MISSING``
===================================================  =========  ==========================

Two of those three are the same status code meaning opposite things, which is the whole
point: the legacy host *answers*, so it looks recoverable, and it is not.

So this module owns two jobs, and no policy of its own - ADR-0002 is the decision, this is
the enforcement and the re-check:

* :func:`state_of` and :func:`guard` - refuse to dial a retired host instead of dialling
  it and misreading the answer. :func:`guard` only binds a transport that can actually
  reach the internet, so the replay fixtures stay usable offline (ADR-0002 §8 makes the
  fixtures the dev and CI source).
* :func:`check_alive` - run ADR-0002's "how to re-verify" recipe from inside the tool, so
  the conditions for re-opening that ADR are a command rather than a shell loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlsplit

from .errors import APIUnavailableError, SourceRetiredError

#: The date Google turned the Fitbit Web API and its OAuth endpoints off (ADR-0002 §1).
LEGACY_SUNSET = "2026-11-02"

#: Every host that belonged to the retired source. ``dev.fitbit.com`` and the plain
#: ``fitbit.com`` API host are in here too: they are all the same retired service, and a
#: user who pasted one of them into ``[api] base_url`` has the same problem.
LEGACY_HOSTS = frozenset(
    {
        "api.fitbit.com",
        "fitbit.com",
        "www.fitbit.com",
        "accounts.fitbit.com",
        "app.fitbit.com",
        "oauth.fitbit.com",
        "dev.fitbit.com",
        "health.api.fitbit.com",
    }
)

#: The source ADR-0002 decided on, and its host - so "is this config the decided source"
#: is a question this module can answer instead of guessing from a URL.
DECIDED_NAME = "Google Health API"
DECIDED_HOST = "health.googleapis.com"
DECIDED_BASE_URL = "https://health.googleapis.com/v4"
DECIDED_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Where the decision and its dated sources live. Named in every message, because the
#: honest answer to "why is this refused" is a document, not a shrug.
DECISION_DOC = "docs/adr/0002-health-api-source.md"

#: The replacement that works today, with no client id and no network.
WORKING_TODAY = "`fm import --source csv --path ARCHIVE` (Google Takeout) needs no API at all"

#: States :attr:`SourceState.state` can take.
#: The configured endpoints belong to the service that was switched off.
STATE_RETIRED = "retired"
#: The endpoints are the ones ADR-0002 decided on.
STATE_DECIDED = "decided"
#: Something else: a proxy, a stand-in, or a hand-edited config.
STATE_CUSTOM = "custom"

#: Verdicts for :func:`check_alive`. Named so a script can branch on them.
ALIVE = "alive"
#: ``410 Gone`` - the retirement the vendor promised, stated out loud.
GONE = "gone"
#: Answered with ``2xx`` to an unauthenticated request: alarming for a data endpoint,
#: normal for a consent page.
UNEXPECTEDLY_OPEN = "unexpectedly_open"
#: A web page that renders without a credential - normal for an authorize endpoint, and
#: exactly as much as it means: the page is up, which says nothing about tokens.
RENDERS = "renders"
#: DNS, TLS, connection refused, timeout.
UNREACHABLE = "unreachable"
#: Anything else (a ``404`` on a docs-era path, a ``5xx``, a redirect loop).
UNEXPECTED = "unexpected"


def host_of(url: str) -> str:
    """The lowercase hostname of ``url``, or ``""`` when it does not parse as one."""
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def is_retired_host(url: str) -> bool:
    """True when ``url`` points at the service that was switched off on 2026-11-02."""
    return host_of(url) in LEGACY_HOSTS


#: The ``[api]`` settings that name a host this build may dial.
ALL_ENDPOINT_SETTINGS = ("base_url", "authorize_url", "token_url")
#: The subset a command that already has a token can dial: it never opens the consent page.
DATA_PLANE_SETTINGS = ("base_url", "token_url")
#: The one a login command dials itself - the consent page is opened by the user's browser,
#: so refusing it would refuse the human rather than the program.
TOKEN_PLANE_SETTINGS = ("token_url",)


@dataclass(frozen=True)
class SourceState:
    """What ``[api]`` points at, and whether that host is still a service."""

    state: str
    #: ``(setting, url)`` for every endpoint this build may dial.
    endpoints: tuple[tuple[str, str], ...] = ()
    #: The subset of :attr:`endpoints` on a retired host.
    retired: tuple[tuple[str, str], ...] = ()

    @property
    def retired_endpoints(self) -> tuple[tuple[str, str], ...]:
        return self.retired

    @property
    def is_retired(self) -> bool:
        return self.state == STATE_RETIRED

    @property
    def hosts(self) -> tuple[str, ...]:
        """Every host this build would dial, in config order, de-duplicated."""
        seen: list[str] = []
        for _setting, url in self.endpoints:
            host = host_of(url)
            if host and host not in seen:
                seen.append(host)
        return tuple(seen)

    def reason(self) -> str:
        """One line naming what is wrong, the date it became wrong, and where it is set.

        Deliberately never suggests re-authenticating: that is the advice this whole
        module exists to withhold, because the retired host answers ``401`` and a
        ``401`` is not evidence about credentials here.
        """
        hosts = ", ".join(sorted({host_of(url) for _name, url in self.retired}))
        settings = ", ".join(f"[api] {name}" for name, _url in self.retired)
        return (
            f"the configured health API source is retired: the Fitbit Web API (hosts: {hosts}) "
            f"was switched off by Google on {LEGACY_SUNSET} and returns no data - it still "
            f"answers, with 401s that look like a bad login and are not - set by {settings}"
        )

    def short(self) -> str:
        """The one-liner for a status line: what the config points at, in verdict form."""
        if self.state == STATE_RETIRED:
            return f"retired (Fitbit Web API, off {LEGACY_SUNSET})"
        if self.state == STATE_DECIDED:
            return f"{DECIDED_NAME} ({DECIDED_HOST})"
        return f"custom ({', '.join(self.hosts) or 'not configured'})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "retired": bool(self.retired),
            "sunset": LEGACY_SUNSET if self.retired else None,
            "endpoints": {name: url for name, url in self.endpoints},
            "retired_endpoints": {name: url for name, url in self.retired_endpoints},
            "hosts": list(self.hosts),
            "decided": {"name": DECIDED_NAME, "base_url": DECIDED_BASE_URL, "token_url": DECIDED_TOKEN_URL},
            "decision": DECISION_DOC,
            "summary": self.short(),
        }

    def __str__(self) -> str:  # pragma: no cover - convenience for log lines
        return self.short()


def state_of(api: Any, *, settings: Sequence[str] = ALL_ENDPOINT_SETTINGS) -> SourceState:
    """Classify an ``[api]`` settings object. Never raises: an unusable URL is ``custom``.

    ``settings`` names which of its URLs count, because what is retired depends on what
    is about to be dialled: a command that already holds a token never opens the consent
    page, so a retired ``authorize_url`` is context for it and a blocker for a login.
    """
    endpoints: list[tuple[str, str]] = []
    for name in settings:
        url = str(getattr(api, name, "") or "").strip()
        if url:
            endpoints.append((name, url))
    retired = tuple((name, url) for name, url in endpoints if is_retired_host(url))
    if retired:
        return SourceState(STATE_RETIRED, tuple(endpoints), retired)
    if host_of(getattr(api, "base_url", "")) == DECIDED_HOST:
        return SourceState(STATE_DECIDED, tuple(endpoints), ())
    return SourceState(STATE_CUSTOM, tuple(endpoints), ())


def guard(
    api: Any,
    *,
    live: bool = True,
    extra: Sequence[str] = (),
    settings: Sequence[str] = ALL_ENDPOINT_SETTINGS,
) -> SourceState:
    """Raise :class:`SourceRetiredError` if this build would dial a retired host.

    Called before a request, never after an answer: the point is to not spend the
    request, and not to inherit the misleading error the dead host returns.

    ``live=False`` is the offline case (a replay fixture directory, a test stand-in) and
    returns without raising - the fixtures are the source CI and ``--fixtures`` use, and
    refusing them would break the one import path that still works. ``extra`` adds URLs
    the caller is about to dial that are not in ``[api]`` (a token endpoint handed in by
    an argument, say), and ``settings`` says which of the ``[api]`` URLs are considered.
    """
    state = state_of(api, settings=settings)
    if not live:
        return state
    for url in extra:
        if is_retired_host(url):
            state = SourceState(
                STATE_RETIRED,
                state.endpoints,
                state.retired + (("endpoint", url),),
            )
            break
    if state.retired:
        raise SourceRetiredError(state.reason())
    return state


@dataclass(frozen=True)
class Observation:
    """One unauthenticated request, what it cost, and what it says about the source."""

    label: str
    url: str
    status: int | None
    #: The most specific token found in the body: an AIP ``status``/``reason``, a Fitbit
    #: ``errorType``, or the first characters of the body. Never a credential.
    signal: str = ""
    verdict: str = UNEXPECTED
    #: What ADR-0002 §2 claimed on 2026-11-22, so drift is visible rather than inferred.
    expected: str = ""

    @property
    def agrees_with_adr(self) -> bool:
        """Whether today's answer still matches the dated claim in the ADR."""
        if self.verdict == UNREACHABLE:
            return False
        if "410" in self.expected:
            return self.verdict == GONE
        if "401" in self.expected:
            return self.status == 401
        if "200" in self.expected:
            return self.status == 200
        return False  # pragma: no cover - every entry names an expectation

    def line(self) -> str:
        status = "-" if self.status is None else str(self.status)
        mark = "" if self.agrees_with_adr else "   (changed since ADR-0002)"
        return f"{self.label:<22} {status:<4} {self.signal:<24} {self.verdict}{mark}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "url": self.url,
            "status": self.status,
            "signal": self.signal,
            "verdict": self.verdict,
            "adr_claim": self.expected,
            "agrees_with_adr": self.agrees_with_adr,
        }


#: The three URLs ADR-0002 measured, with what the ADR recorded for each. Kept next to
#: the code so a reader who wonders "is the legacy host still answering?" can find out
#: with one command instead of a curl loop and a memory for status codes.
#: ``(label, url, what ADR-0002 measured there, kind)`` - ``kind`` is ``api`` for a data
#: endpoint (where ``200`` without a credential would be a scandal) or ``web`` for a page
#: (where it is just a page).
VERIFY_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    ("legacy api", "https://api.fitbit.com/1/user/-/profile.json", "401 (treat as dead)", "api"),
    ("legacy oauth", "https://accounts.fitbit.com/oauth2/authorize?client_id=x", "200 (renders, no tokens)", "web"),
    (
        "decided api",
        f"{DECIDED_BASE_URL}/users/me/dataTypes/heart-rate/dataPoints",
        "401 (alive, wants a Google token)",
        "api",
    ),
)


def check_alive(
    transport: Any, *, targets: Sequence[tuple[str, str, str, str]] | None = None
) -> tuple[Observation, ...]:
    """Ask each target what it is, with no credentials, and classify the answer.

    This is ADR-0002's "how to re-verify" recipe, kept honest by comparing against what
    the ADR recorded: a legacy host that has started answering ``410``, or a decided host
    that answers ``2xx`` without a token, are both events that re-open that decision, and
    both are visible here as ``agrees_with_adr=False``. Costs one request per target;
    needs no client id, no token and no account.
    """
    observations: list[Observation] = []
    for label, url, expected, kind in targets or VERIFY_TARGETS:
        try:
            response = transport.request("GET", url, {"Accept": "application/json"}, None)
        except APIUnavailableError as exc:
            observations.append(Observation(label, url, None, str(exc)[:80], UNREACHABLE, expected))
            continue
        observations.append(
            Observation(
                label=label,
                url=url,
                status=response.status,
                signal=_signal(response),
                verdict=_verdict(response.status, kind),
                expected=expected,
            )
        )
    return tuple(observations)


def _verdict(status: int, kind: str = "api") -> str:
    if status == 410:
        return GONE
    if status in (401, 403):
        # Wants a credential: the endpoint exists. Whether the service behind it is a
        # service any more is what `state_of` answers, not this function.
        return ALIVE
    if 200 <= status < 300:
        return RENDERS if kind == "web" else UNEXPECTEDLY_OPEN
    return UNEXPECTED


def _signal(response: Any) -> str:
    """The most specific thing the body says - a code, not a dump.

    Both error dialects this source has ever spoken are read: Google's AIP-193 envelope
    (``{"error": {"status": ..., "details": [{"reason": ...}]}}``) and the old Fitbit
    array (``{"errors": [{"errorType": ...}]}``). Nothing else from the body survives
    this function, so an error page with someone's data in it cannot end up in a report.
    """
    body = getattr(response, "body", b"") or b""
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        parts = [str(error.get("status") or error.get("code") or "")]
        for detail in error.get("details") or []:
            if isinstance(detail, dict):
                metadata = detail.get("metadata")
                reason = str((metadata or {}).get("reason") or detail.get("reason") or "")
                if reason:
                    parts.append(reason)
                    break
        return " ".join(part for part in parts if part)[:80]
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        return str(first.get("errorType") or first.get("message") or "")[:80]
    return str(payload.get("error") or "")[:80]


__all__ = [
    "ALIVE",
    "DECIDED_BASE_URL",
    "DECIDED_HOST",
    "DECIDED_NAME",
    "DECIDED_TOKEN_URL",
    "DECISION_DOC",
    "GONE",
    "LEGACY_HOSTS",
    "LEGACY_SUNSET",
    "Observation",
    "RENDERS",
    "STATE_CUSTOM",
    "STATE_DECIDED",
    "STATE_RETIRED",
    "SourceState",
    "TOKEN_PLANE_SETTINGS",
    "DATA_PLANE_SETTINGS",
    "ALL_ENDPOINT_SETTINGS",
    "UNEXPECTED",
    "UNEXPECTEDLY_OPEN",
    "UNREACHABLE",
    "VERIFY_TARGETS",
    "WORKING_TODAY",
    "check_alive",
    "guard",
    "host_of",
    "is_retired_host",
    "state_of",
]
