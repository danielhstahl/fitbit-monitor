"""``fm api probe``: ask the API why an import would land nothing.

The most common failure of a new setup is not a crash, it is a quiet zero: ``fm import``
runs, stores nothing, and the reason is one of four things - a scope the token was never
given, a dataset the account does not have (no Pulse Ox, no tracker), a rate limit, or an
endpoint that answered something unexpected. Every one of them is answerable by *asking
the API*, which is what this module does in a bounded number of requests:

1. **Who am I** - one ``users/me/profile`` call (membership date, age) and one
   ``users/me/identity`` call (the ``healthUserId``, and the Fitbit-era id next to it, so
   a ``client_user_mismatch`` is legible). No measurements are copied out of either - see
   :mod:`fitbit_monitor.logsafe`.
2. **What may I see** - the token's granted scopes against the scopes the catalog wants,
   naming each missing one and the datasets it disables.
3. **What is there** - one request per dataset for the newest day, then a *doubling walk*
   back through history (1, 2, 4, 8 ... days, then the floor) so the earliest date of
   available data is bounded in ``log2(days)`` requests instead of one per day.
4. **What is left** - whatever rate-limit headers turned up along the way (the v4 API
   sends none, and saying so is itself a finding), and the local ceiling the client is
   holding itself to.

Two rules make this usable as a diagnostic rather than a stack trace:

* **Nothing here raises.** Every transport and HTTP failure becomes a status on the
  dataset it happened to (``forbidden``, ``no_data``, ``rate_limited``, ``unreachable``,
  ``unknown``), so one bad endpoint still reports the other four.
* **Nothing here guesses.** A date reported as "the earliest available" carries how it
  was established (:attr:`DatasetProbe.history`) - found by walking back to an empty day,
  or a floor that data reaches, or cut short by the request budget. A diagnostic that
  states a number it did not earn is worse than one that says "at least".

The report is data first (:meth:`ProbeReport.as_dict`) so ``fm doctor`` and shell
scripts can read it, with :meth:`ProbeReport.lines` as the human view of the same facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..timeutil import utcnow
from .catalog import ALL_SCOPES, CATALOG, Dataset, Payload, lookup, perform, short_scope
from .client import FitbitClient
from .errors import APIUnavailableError, FitbitAPIError, HTTPError, RateLimitError

#: Dataset statuses. Only :data:`DATA` means "this dataset will import".
DATA = "data"
#: 404/410 - the endpoint works and there is nothing for the days asked.
NO_DATA = "no_data"
#: 2xx with a body that yielded no readable rows (unexpected shape, or an empty day).
EMPTY = "empty"
#: 403 - the request was allowed through auth but refused for this app or account.
FORBIDDEN = "forbidden"
#: 401 - the token was refused outright.
UNAUTHORIZED = "unauthorized"
#: 429, or a rate limit the client refused to sit and wait for.
RATE_LIMITED = "rate_limited"
#: 5xx.
SERVER_ERROR = "server_error"
#: Could not reach the API at all (DNS, TLS, timeout).
UNREACHABLE = "unreachable"
#: The scope the dataset needs is not on the token, so it was never asked for.
NO_SCOPE = "no_scope"
#: The request budget ran out before this dataset was asked anything.
NO_BUDGET = "no_budget"
#: Anything else, with the reason in ``message``.
UNKNOWN = "unknown"

#: How well :attr:`DatasetProbe.earliest` is bounded - the honesty field.
HISTORY_NOT_SEARCHED = "not_searched"
HISTORY_NONE = "no_data"
#: Every sample back to the floor had data, so data reaches at least that far.
HISTORY_FLOOR = "account_floor"
#: An older sample came back empty, so the data starts between that day and ``earliest``.
HISTORY_BOUNDED = "bounded"
#: The walk stopped at the request budget, not at an empty day.
HISTORY_BUDGET = "budget"

#: Requested scopes the tool has no use for, reported as information only.
_DEFAULT_BACKFALL_DAYS = 365

#: A probe is a diagnostic, not a backfill: it stops here by default. The doubling walk
#: costs about ``log2(window)`` requests per dataset (two per sample for datasets whose
#: day comes from two endpoints), so a four-year account fits in this budget.
DEFAULT_MAX_REQUESTS = 60

#: HTTP status -> our status name.
_BY_HTTP = {
    401: UNAUTHORIZED,
    403: FORBIDDEN,
    404: NO_DATA,
    410: NO_DATA,
    429: RATE_LIMITED,
}
#: Worse-last ordering, used when one day costs several requests and they disagree.
_SEVERITY = {
    DATA: 0,
    NO_BUDGET: 0,
    NO_DATA: 1,
    EMPTY: 2,
    RATE_LIMITED: 3,
    SERVER_ERROR: 4,
    UNREACHABLE: 5,
    UNAUTHORIZED: 6,
    FORBIDDEN: 7,
    UNKNOWN: 8,
}


def classify_status(status: int) -> str:
    """The probe's name for an HTTP status (everything unrecognised is ``unknown``)."""
    named = _BY_HTTP.get(status)
    if named is not None:
        return named
    return SERVER_ERROR if status >= 500 else UNKNOWN


@dataclass(frozen=True)
class ScopeGap:
    """One scope the tool wants that this token does not have."""

    scope: str
    datasets: tuple[str, ...] = ()
    requested: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "datasets": list(self.datasets),
            "requested_at_login": self.requested,
        }

    def line(self) -> str:
        disabled = ", ".join(self.datasets) or "no dataset in the catalog (profile.readonly is identity only)"
        extra = "" if self.requested else " - never asked for: set [api] scopes, then run `fm auth login`"
        return f"missing {short_scope(self.scope)} - disables: {disabled}{extra}"


@dataclass
class DatasetProbe:
    """What one dataset answered, and how far the probe looked for it."""

    dataset: str
    scope: str
    status: str = UNKNOWN
    http_status: int | None = None
    requests: int = 0
    samples: int = 0
    earliest: date | None = None
    latest: date | None = None
    history: str = HISTORY_NOT_SEARCHED
    #: Newest sampled day that came back empty while walking back (the lower bound).
    starts_after: date | None = None
    #: The floor the walk reached, when it reached one (member-since, ``--since``...).
    floor: str = ""
    message: str = ""

    @property
    def usable(self) -> bool:
        return self.status == DATA

    @property
    def reason(self) -> str:
        """Short, human, and always about *this* dataset (every path writes one)."""
        return self.message or self.status

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "scope": self.scope,
            "status": self.status,
            "usable": self.usable,
            "http_status": self.http_status,
            "requests": self.requests,
            "samples": self.samples,
            "earliest": self.earliest.isoformat() if self.earliest else None,
            "latest": self.latest.isoformat() if self.latest else None,
            "history": self.history,
            "starts_after": self.starts_after.isoformat() if self.starts_after else None,
            "floor": self.floor or None,
            "message": self.reason,
        }


@dataclass
class ProbeReport:
    """Everything one probe learned, as data."""

    generated: datetime
    granted_scopes: tuple[str, ...] = ()
    wanted_scopes: tuple[str, ...] = ALL_SCOPES
    requested_scopes: tuple[str, ...] = ALL_SCOPES
    missing_scopes: tuple[ScopeGap, ...] = ()
    unused_scopes: tuple[str, ...] = ()
    account: dict[str, Any] = field(default_factory=dict)
    datasets: tuple[DatasetProbe, ...] = ()
    rate_limit: dict[str, Any] = field(default_factory=dict)
    requests: int = 0
    since: date | None = None
    until: date | None = None
    history: bool = True
    profile_error: str | None = None
    notes: list[str] = field(default_factory=list)

    # --- verdicts -----------------------------------------------------------
    @property
    def usable(self) -> tuple[DatasetProbe, ...]:
        return tuple(item for item in self.datasets if item.usable)

    @property
    def blocked(self) -> tuple[DatasetProbe, ...]:
        """Datasets that will not import: refused, missing a grant, or empty."""
        return tuple(item for item in self.datasets if not item.usable)

    @property
    def needs_attention(self) -> bool:
        """True unless every dataset the catalog knows answered with data."""
        return bool(self.blocked)

    @property
    def exit_code(self) -> int:
        """0 everything available, 5 something is unavailable, 1 nothing is.

        The same ladder ``fm import`` uses (1 total failure, 5 "worked, with warnings"),
        so a cron line can treat the two the same way.
        """
        if not self.usable:
            return 1
        return 5 if self.needs_attention else 0

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for item in self.datasets:
            tally[item.status] = tally.get(item.status, 0) + 1
        return tally

    # --- output -------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": "fm api probe",
            "report_version": 1,
            "generated": self.generated.isoformat(timespec="seconds"),
            "authenticated": True,
            "exit_code": self.exit_code,
            "account": dict(self.account),
            "window": {
                "since": self.since.isoformat() if self.since else None,
                "until": self.until.isoformat() if self.until else None,
                "history": self.history,
            },
            "scopes": {
                "granted": list(self.granted_scopes),
                "wanted": list(self.wanted_scopes),
                "requested_at_login": list(self.requested_scopes),
                "missing": [gap.as_dict() for gap in self.missing_scopes],
                "unused": list(self.unused_scopes),
            },
            "profile_error": self.profile_error,
            "datasets": [item.as_dict() for item in self.datasets],
            "counts": self.counts(),
            "rate_limit": dict(self.rate_limit),
            "requests": self.requests,
            "notes": list(self.notes),
        }

    def lines(self) -> list[str]:
        """The human report: the same facts, in the order a person reads them."""
        out: list[str] = []
        account = self.account
        named = account.get("health_user_id") or account.get("legacy_user_id") or "unknown account"
        since_text = f", member since {account['member_since']}" if account.get("member_since") else ""
        out.append(f"account   {named}{since_text}")
        if account.get("legacy_user_id"):
            out.append(f"          fitbit-era id: {account['legacy_user_id']}")
        if self.profile_error:
            out.append(f"          profile probe failed: {self.profile_error}")
        out.append(f"scopes    granted: {', '.join(short_scope(s) for s in self.granted_scopes) or '-'}")
        out.append(f"          wanted:  {', '.join(short_scope(s) for s in self.wanted_scopes) or '-'}")
        for gap in self.missing_scopes:
            out.append(f"          {gap.line()}")
        if self.unused_scopes:
            out.append(f"          unused: {', '.join(short_scope(s) for s in self.unused_scopes)}")
        out.append(f"datasets  {self.since or '-'} .. {self.until or '-'}")
        # Sized to the scopes actually in this report: the v4 names are long enough that
        # a fixed column either runs them into the next field or wastes half a terminal.
        scope_width = max((len(short_scope(item.scope)) for item in self.datasets), default=12) + 1
        for item in self.datasets:
            out.append(
                f"  [{item.status}]".ljust(16)
                + f"{item.dataset:<15}{short_scope(item.scope):<{scope_width}}{item.reason}"
            )
        out.append(f"rate limit {_rate_limit_line(self.rate_limit)}")
        out.append(f"requests  {self.requests}")
        out.extend(f"note: {note}" for note in self.notes)
        return out


# --- the probe itself --------------------------------------------------------
class _Budget:
    """Shared request budget, so a probe cannot sprawl across the rate-limit window."""

    def __init__(self, limit: int | None, used: int = 0) -> None:
        self.limit = limit
        self.used = used

    def left(self) -> int | None:
        return None if self.limit is None else max(0, int(self.limit) - self.used)

    def can(self, want: int = 1) -> bool:
        left = self.left()
        return left is None or left >= want

    def spend(self, count: int = 1) -> int:
        """Charge ``count`` requests and hand back the total spent so far."""
        self.used += count
        return self.used


@dataclass
class _Sample:
    """One day (or one window) asked of one endpoint."""

    status: str
    http_status: int | None = None
    days: tuple[date, ...] = ()
    message: str = ""


class _HeaderRecorder:
    """Transport wrapper that keeps the rate-limit headers it has *seen*, verbatim.

    The client parses them into :class:`~fitbit_monitor.api.client.RateLimit`, and that
    is the right thing for import decisions; a diagnostic also wants the raw header names,
    because "we expected ``X-RateLimit-Remaining`` and the API sent something else" is
    itself an answer - and it is the only way to report headers on a response that came
    back as an exception.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, headers=None, body=None):  # type: ignore[no-untyped-def]
        response = self.inner.request(method, url, headers, body)
        for name, value in response.headers.items():
            if name.lower().startswith("x-ratelimit") or name.lower() == "retry-after":
                self.headers[name] = str(value)
        return response


def run_probe(
    client: FitbitClient,
    *,
    datasets: Sequence[str] | None = None,
    since: date | None = None,
    until: date | None = None,
    history: bool = True,
    max_requests: int | None = DEFAULT_MAX_REQUESTS,
    timezone: str | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> ProbeReport:
    """Probe the account behind ``client`` and report what it found.

    ``datasets`` narrows the probe (validated against the catalog); ``since``/``until``
    bound the history walk (default: the account's ``memberSinceDate`` to today);
    ``history=False`` skips the walk and asks only the newest day; ``max_requests``
    caps the whole probe, including the retries the client is allowed.

    Raises only :class:`~fitbit_monitor.api.errors.NotAuthenticatedError` (there is no
    token to ask with) and :class:`~fitbit_monitor.errors.UsageError` (an unknown dataset
    name); everything the *API* does is reported in the result.
    """
    granted = tuple(client.tokens.scopes)
    names = _resolve(datasets)
    timezone = timezone or client.settings.timezone
    # Not `or ALL_SCOPES` inline: a configured scope this build cannot map is a config
    # problem, and reporting a narrowed "requested" list as if it were the plan would hide
    # the one thing the user has to fix.
    requested = client.api.scopes_to_request()

    recorder = _HeaderRecorder(client.transport)
    client.transport = recorder
    budget = _Budget(max_requests)
    try:
        profile, profile_error = _read_profile(client, budget=budget)
        identity = _read_identity(client, budget=budget)
        user = _user_view(profile)
        until_day = until or clock().date()
        member_since = _as_date(user.get("membershipStartDate") or user.get("memberSinceDate"))
        since_day = since or member_since or (until_day - timedelta(days=_DEFAULT_BACKFALL_DAYS - 1))
        if since_day > until_day:
            since_day = until_day

        floor = _floor_source(since, since_day, member_since)
        probes: list[DatasetProbe] = []
        for name in names:
            dataset = CATALOG[name]
            probes.append(
                _probe_dataset(
                    client,
                    dataset,
                    granted=granted,
                    since=since_day,
                    until=until_day,
                    history=history,
                    budget=budget,
                    timezone=timezone,
                    floor=floor,
                )
            )
        notes: list[str] = []
        if profile_error:
            notes.append(
                "profile probe failed, so the account's membership date could not be read: " + profile_error
            )
        if not identity:
            notes.append("users/me/identity answered nothing, so no user id is reported")
        if not history:
            notes.append("--no-history: the earliest date is the newest day asked, not a bound on the data")
        if budget.limit is not None and not budget.can(1):
            notes.append(
                f"the --max-requests budget ({budget.limit}) ran out; re-run with a larger one for tighter bounds"
            )
        report = ProbeReport(
            generated=clock(),
            granted_scopes=granted,
            wanted_scopes=ALL_SCOPES,
            requested_scopes=requested,
            missing_scopes=_missing_scopes(granted, requested, names),
            unused_scopes=tuple(sorted(set(granted) - set(ALL_SCOPES))),
            account=_account_view(identity, user, member_since),
            datasets=tuple(probes),
            rate_limit=_rate_limit(client, recorder, clock()),
            requests=budget.used,
            since=since_day,
            until=until_day,
            history=history,
            profile_error=profile_error,
            notes=notes,
        )
        return report
    finally:
        client.transport = recorder.inner


# --- internals ---------------------------------------------------------------
def _resolve(datasets: Sequence[str] | None) -> tuple[str, ...]:
    """Dataset names to probe, validated against the catalog (default: all of them)."""
    if not datasets:
        return tuple(sorted(CATALOG))
    out: list[str] = []
    for item in datasets:
        for part in str(item).split(","):
            part = part.strip()
            if part and part not in out:
                lookup(part)
                out.append(part)
    return tuple(out)


def _read_profile(client: FitbitClient, *, budget: _Budget) -> tuple[dict[str, Any], str | None]:
    """``users/me/profile`` as a dict, plus the reason it was not a usable one.

    A failure here is reported, not fatal: the profile needs ``profile.readonly``, which a
    user may have declined, and every question the probe asks about *data* is answered by
    the dataset endpoints instead.
    """
    budget.spend()
    try:
        payload = client.profile()
    except FitbitAPIError as exc:
        return {}, str(exc)
    if not payload:
        # Empty is what :meth:`HealthClient.profile` hands back for a body that was not
        # an object - a 200 that told us nothing is still worth saying out loud.
        return {}, "users/me/profile answered with an empty or unrecognisable payload"
    return payload, None


def _read_identity(client: FitbitClient, *, budget: _Budget) -> dict[str, Any]:
    """``users/me/identity``, or ``{}``. Any health scope answers this one.

    Worth the request on its own: it is the only place the numeric ``healthUserId`` and
    the Fitbit-era ``legacyUserId`` appear, which is how a ``client_user_mismatch`` on a
    Personal-mode project becomes a message instead of a mystery.
    """
    if not budget.can():
        return {}
    budget.spend()
    try:
        return client.identity()
    except FitbitAPIError:
        return {}


def _account_view(identity: Mapping[str, Any], user: Mapping[str, Any], member_since: date | None) -> dict[str, Any]:
    """Who this token belongs to, as a diagnostic reports it.

    Ids, the membership date and the time zone: the three things that explain a window.
    Deliberately not the rest of the profile, which also carries age, date of birth,
    height and weight - a diagnostic gets printed on a screen in someone else's office.
    """
    return {
        "health_user_id": str(identity.get("healthUserId") or "") or None,
        "legacy_user_id": str(identity.get("legacyUserId") or "") or None,
        "member_since": member_since.isoformat() if member_since else None,
        "display_name": user.get("displayName") or None,
        "time_zone": user.get("timeZone") or None,
    }


def _user_view(profile: dict[str, Any]) -> dict[str, Any]:
    """The part of the profile that describes the account (older shape: nested)."""
    inner = profile.get("user")
    return inner if isinstance(inner, dict) else profile


def _as_date(value: Any) -> date | None:
    """An ISO string *or* a ``google.type.Date`` - v4 uses the latter."""
    if isinstance(value, Mapping):
        try:
            return date(int(value["year"]), int(value["month"]), int(value["day"]))
        except (KeyError, TypeError, ValueError):
            return None
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _missing_scopes(granted: Iterable[str], requested: Iterable[str], names: Sequence[str]) -> tuple[ScopeGap, ...]:
    """Each wanted scope that is missing, with the datasets it disables."""
    have = set(granted)
    asked = set(requested)
    gaps: list[ScopeGap] = []
    for scope in ALL_SCOPES:
        if scope in have:
            continue
        # ``profile.readonly`` disables no dataset of its own; it is the identity probe,
        # and its absence is reported as a profile_error instead.
        disabled = tuple(name for name in names if scope in CATALOG[name].scopes)
        gaps.append(ScopeGap(scope=scope, datasets=disabled, requested=scope in asked))
    return tuple(gaps)


def _probe_dataset(
    client: FitbitClient,
    dataset: Dataset,
    *,
    granted: tuple[str, ...],
    since: date,
    until: date,
    history: bool,
    budget: _Budget,
    timezone: str,
    floor: str = "",
) -> DatasetProbe:
    probe = DatasetProbe(dataset=dataset.name, scope=dataset.scope)
    missing = [scope for scope in dataset.scopes if scope not in granted]
    if granted and missing:
        # The row names the scope the refusal is *about*, not the dataset's first scope:
        # daily_summary needs two, and showing the one that was granted next to "token has
        # no ..." reads like a contradiction.
        probe.scope = missing[0]
        probe.status = NO_SCOPE
        probe.message = f"token has no {short_scope(missing[0])!r} scope; not asked for"
        return probe
    if not budget.can():
        probe.status = NO_BUDGET
        probe.message = "request budget spent before this dataset was asked"
        return probe

    days = _sample_days(until, since) if history else [until]
    observed: list[date] = []
    failure: _Sample | None = None
    stopped_at: tuple[date, _Sample] | None = None
    cut_short: str | None = None
    spent_before = budget.used
    for index, day in enumerate(days):
        cost = len(dataset.requests_in_chunk(day, day))
        if not budget.can(cost):
            cut_short = HISTORY_BUDGET if index else NO_BUDGET
            break
        sample = _ask(client, dataset, day, budget=budget, timezone=timezone)
        probe.samples += 1
        if sample.status == DATA and probe.http_status is None:
            probe.http_status = sample.http_status
        elif index == 0 and probe.http_status is None:
            probe.http_status = sample.http_status
        if sample.status == DATA:
            observed.extend(sample.days)
            continue
        if observed:
            # Data at a newer day, nothing at this older one: the boundary is between
            # the two, which is the answer the walk was for.
            stopped_at = (day, sample)
            break
        failure = failure or sample
        if _systemic(sample.status):
            # A 403 or an unreachable host will not get better further back in time.
            break
    probe.requests = budget.used - spent_before
    probe.floor = floor
    if not observed:
        probe.status = failure.status if failure else UNKNOWN
        probe.http_status = probe.http_status or (failure.http_status if failure else None)
        probe.message = failure.message if failure and failure.message else probe.status
        probe.history = HISTORY_NONE if history else HISTORY_NOT_SEARCHED
        if cut_short == NO_BUDGET:
            probe.status = NO_BUDGET
            probe.message = "request budget spent before this dataset was asked"
        return probe
    probe.status = DATA
    probe.earliest = min(observed)
    probe.latest = max(observed)
    if not history:
        probe.history = HISTORY_NOT_SEARCHED
        detail = f"data {probe.earliest} .. {probe.latest} (newest day asked only)"
    elif cut_short == HISTORY_BUDGET:
        probe.history = HISTORY_BUDGET
        detail = f"data {probe.earliest} .. {probe.latest}; older data not searched (--max-requests spent)"
    elif stopped_at:
        day, sample = stopped_at
        probe.starts_after = day
        probe.history = HISTORY_BOUNDED
        detail = (
            f"data {probe.earliest} .. {probe.latest}; nothing as far back as {day} ({sample.status}"
            + (f", {sample.message}" if sample.message else "")
            + ")"
        )
    else:
        probe.history = HISTORY_FLOOR
        detail = (
            f"data {probe.earliest} .. {probe.latest}; sampled back to {since} ({probe.floor}), "
            "every sampled day had data"
        )
    probe.message = detail
    return probe


def _systemic(status: str) -> bool:
    """True for the answers that would repeat on every other day of the same dataset."""
    return status in (FORBIDDEN, UNAUTHORIZED, UNREACHABLE, RATE_LIMITED, SERVER_ERROR, NO_BUDGET)


def _floor_source(since: date | None, since_day: date, member_since: date | None) -> str:
    """Where the lower end of the history walk came from - part of every claim it makes."""
    if since is not None:
        return f"--since {since.isoformat()}"
    if member_since is not None:
        return f"membershipStartDate {member_since.isoformat()}"
    return f"{_DEFAULT_BACKFALL_DAYS}d default window (--since to widen it)"


def _sample_days(until: date, since: date) -> tuple[date, ...]:
    """Days to ask, newest first: a doubling walk back, then ``since`` itself.

    ``log2`` requests cover four years of history, and the floor is always asked so a
    "data goes back this far" claim is an observation rather than an inference.
    """
    if until <= since:
        return (until,)
    days = [until]
    step = 1
    while until - timedelta(days=step) > since:
        days.append(until - timedelta(days=step))
        step *= 2
    days.append(since)
    return tuple(days)


def _ask(
    client: FitbitClient,
    dataset: Dataset,
    day: date,
    *,
    budget: _Budget,
    timezone: str,
) -> _Sample:
    """Ask one dataset for one day, and classify the answer without raising."""
    requests = tuple(dataset.requests_in_chunk(day, day))
    payloads: list[Payload] = []
    statuses: list[str] = []
    http_status: int | None = None
    messages: list[str] = []
    for request in requests:
        path = request.path()
        budget.spend()
        try:
            response = perform(client, request)
        except HTTPError as exc:
            statuses.append(classify_status(exc.status))
            http_status = exc.status
            messages.append(_detail(exc))
            continue
        except RateLimitError as exc:
            statuses.append(RATE_LIMITED)
            messages.append(_detail(exc))
            break
        except APIUnavailableError as exc:
            statuses.append(UNREACHABLE)
            messages.append(_detail(exc))
            break
        except FitbitAPIError as exc:
            statuses.append(UNKNOWN)
            messages.append(_detail(exc))
            continue
        body = _json_dict(response)
        if body is None:
            statuses.append(EMPTY if response.ok else classify_status(response.status))
            http_status = response.status
            messages.append(f"{path}: {response.status} with no JSON object to read")
            continue
        payloads.append(Payload(day=day, body=body))
        http_status = response.status
    if payloads:
        try:
            rows = dataset.normalize(payloads, timezone)
        except Exception as exc:  # noqa: BLE001 - an unreadable shape is a finding, not a crash
            return _Sample(UNKNOWN, http_status, (), f"{dataset.name}: could not read the response ({_detail(exc)})")
        days = tuple(sorted({row[0].date() for row in rows}))
        if days:
            return _Sample(DATA, http_status, days, "")
        statuses.append(EMPTY)
        messages.append(f"{dataset.name}: HTTP {http_status} carried no readable {dataset.name} rows")
    worst = max(statuses or [EMPTY], key=lambda name: _SEVERITY.get(name, _SEVERITY[UNKNOWN]))
    return _Sample(worst, http_status, (), "; ".join(messages[:2]))


def _detail(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return text[:200]


def _json_dict(response: Any) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a body that is not JSON is a finding, not a crash
        return None
    return payload if isinstance(payload, dict) else None


def _rate_limit(client: FitbitClient, recorder: _HeaderRecorder, now: datetime) -> dict[str, Any]:
    """What the API said about its rate limit, parsed and verbatim.

    For ``v4`` the honest answer is usually "nothing": the API publishes a per-minute
    quota but no ``X-RateLimit-*`` headers, so what the client *did* to keep inside it -
    the local ceiling in ``[api] requests_per_second`` - is reported next to what it was
    told. A diagnostic that only prints the headers would print four blanks and leave the
    user to conclude that nothing is limiting the import.
    """
    reading = client.rate_limit
    out: dict[str, Any] = {
        "limit": reading.limit,
        "remaining": reading.remaining,
        "interval": reading.scope,
        "reset_in_seconds": None if reading.reset_in is None else round(reading.reset_in, 1),
        "reset_at": None,
        "observed_headers": dict(recorder.headers),
        "local_requests_per_second": getattr(client.api, "requests_per_second", None),
        "local_waits": getattr(client.bucket, "waits", 0),
    }
    if reading.reset_in is not None:
        out["reset_at"] = (now + timedelta(seconds=float(reading.reset_in))).isoformat(timespec="seconds")
    return out


def _rate_limit_line(rate_limit: Mapping[str, Any]) -> str:
    limit = rate_limit.get("limit")
    remaining = rate_limit.get("remaining")
    interval = rate_limit.get("interval")
    if limit is None and remaining is None:
        local = rate_limit.get("local_requests_per_second")
        local_text = "" if local in (None, float("inf")) else f"; this client holds itself to {local:g} requests/second"
        return f"no ceiling sent by the API (v4 sends no X-RateLimit-* headers){local_text}"
    left = f"{remaining} of {limit}" if limit is not None and remaining is not None else str(remaining or limit)
    text = f"{left} requests left"
    if interval:
        text += f" in the {interval} window"
    reset_in = rate_limit.get("reset_in_seconds")
    if reset_in is not None:
        text += f"; resets in {int(reset_in)}s"
        if rate_limit.get("reset_at"):
            text += f" (at {rate_limit['reset_at']})"
    return text


__all__ = [
    "DATA",
    "DEFAULT_MAX_REQUESTS",
    "EMPTY",
    "FORBIDDEN",
    "HISTORY_BOUNDED",
    "HISTORY_BUDGET",
    "HISTORY_FLOOR",
    "HISTORY_NONE",
    "HISTORY_NOT_SEARCHED",
    "NO_BUDGET",
    "NO_DATA",
    "NO_SCOPE",
    "ProbeReport",
    "RATE_LIMITED",
    "SERVER_ERROR",
    "UNAUTHORIZED",
    "UNREACHABLE",
    "UNKNOWN",
    "DatasetProbe",
    "ScopeGap",
    "classify_status",
    "run_probe",
]
