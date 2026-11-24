"""The HTTP layer: one small response type and three interchangeable transports.

Everything above this module (the client, the catalog, the importer) only ever sees
:meth:`Transport.request`, which is what makes the whole import path testable with
no network and no secrets:

* :class:`UrllibTransport` - the real one, stdlib ``urllib`` only.
* :class:`ReplayTransport` - plays back recorded fixtures from a directory, and
  raises :class:`~fitbit_monitor.api.errors.ReplayMissError` on anything that was
  never recorded, so fixtures cannot silently drift from the code.
* :class:`RecordingTransport` - wraps another transport and writes what it saw to a
  fixture directory (that is how ``tests/fixtures/api`` is built, offline).

Keeping the transports this dumb is deliberate: retries, rate limits and ETags live
in :mod:`fitbit_monitor.api.client`, so they are exercised against the replay
transport exactly as they are against the live API.

Each transport also answers one question about itself - ``live``, "can this thing reach
the public internet" - because that is what decides whether the retired-source guard in
:mod:`fitbit_monitor.api.source` applies. Only :class:`UrllibTransport` can, so only it
says yes; a replay directory, and any transport a test hands in, says no and keeps its
freedom to talk to a stand-in. A transport that does not answer at all is read as not
live, which is the safe direction: it is a stand-in by construction.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from hashlib import sha1
from pathlib import Path
from typing import Any, Mapping

from .errors import APIUnavailableError, ReplayMissError

USER_AGENT = "fitbit-monitor/0.1 (+local-first; no external services)"


@dataclass(frozen=True)
class Response:
    """One HTTP response: status, case-insensitive headers, bytes."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str, default: str | None = None) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def json(self) -> Any:
        """Parsed body. Empty bodies parse as ``{}`` - a 204 is not an error."""
        if not self.body.strip():
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise APIUnavailableError(f"response was not JSON: {exc}") from exc

    @property
    def text(self) -> str:
        """Body decoded for error messages and raw retention."""
        return self.body.decode("utf-8", errors="replace")


def body_signature(body: bytes | Mapping[str, Any] | None) -> dict[str, str]:
    """What distinguishes one POST of a ``rollUp`` from another, as readable key parts.

    A GET says what it wants in its query string; a rollup says it in a JSON body, so a
    fixture key that ignores the body folds five different windows onto one file and a
    replay silently answers the wrong one. The window and the aggregation are pulled out
    *as text* rather than hashed into a digest, because a fixture directory you cannot
    read is a fixture directory nobody reviews.
    """
    if body is None:
        return {}
    if isinstance(body, (bytes, bytearray)):
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"body": sha1(bytes(body)).hexdigest()[:12]}
    elif isinstance(body, Mapping):
        payload = dict(body)
    else:
        return {}
    # A JSON body that is a list or a scalar has no window to name; it still needs a
    # stable signature, so it keeps the digest of its bytes.
    data: dict[str, Any] = dict(payload) if isinstance(payload, Mapping) else {}
    out: dict[str, str] = {}
    span = data.get("range")
    if isinstance(span, Mapping):
        start, end = span.get("startTime"), span.get("endTime")
        if start or end:
            out["from"], out["to"] = str(start or ""), str(end or "")
        civil_start, civil_end = span.get("start"), span.get("end")
        if civil_start or civil_end:
            out["from"] = _civil_text(civil_start)
            out["to"] = _civil_text(civil_end)
    for key, name in (("windowSize", "window"), ("windowSizeDays", "window"), ("pageSize", "pageSize")):
        if data.get(key) is not None:
            out[name] = str(data[key])
    if data.get("pageToken"):
        out["pageToken"] = str(data["pageToken"])
    if not out:
        return {"body": sha1(bytes(body) if isinstance(body, (bytes, bytearray)) else str(body).encode()).hexdigest()[:12]}
    return out


def _civil_text(value: Any) -> str:
    """A ``google.type.CivilDate`` in a body, as the shortest thing that identifies it."""
    if isinstance(value, Mapping):
        inner = value.get("date")
        days: Mapping[str, Any] = inner if isinstance(inner, Mapping) else value
        try:
            return "%04d-%02d-%02d" % (int(days["year"]), int(days["month"]), int(days["day"]))
        except (KeyError, TypeError, ValueError):
            return json.dumps(value, sort_keys=True)
    return str(value or "")


def fixture_key(
    method: str,
    path: str,
    query: Mapping[str, Any] | None = None,
    body: bytes | Mapping[str, Any] | None = None,
) -> str:
    """The name of the fixture for a request, independent of query ordering."""
    normalized = path if path.startswith("/") else f"/{path}"
    key = f"{method.upper()} {normalized}"
    parts = dict(query or {})
    parts.update(body_signature(body))
    if parts:
        encoded = urllib.parse.urlencode(sorted((k, str(v)) for k, v in parts.items()))
        if encoded:
            key = f"{key}?{encoded}"
    return key


def fixture_filename(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "_", key).strip("_") + ".json"


def load_fixtures(directory: Path) -> dict[str, dict]:
    """Every fixture in ``directory``, keyed by :func:`fixture_key`.

    A file whose name starts with ``_`` is metadata about the directory, not a fixture
    (``_provenance.json``: what generated the corpus, over what window, from what seed),
    and is skipped. Every other ``.json`` has to be a fixture, so a stray file there is a
    replay miss rather than a silence.
    """
    out: dict[str, dict] = {}
    for path in sorted(p for p in Path(directory).rglob("*.json") if not p.name.startswith("_")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ReplayMissError(f"{path}: not valid fixture JSON ({exc})") from exc
        request = record.get("request") or {}
        if "key" in record:  # hand-written fixtures may name themselves
            key = str(record["key"])
        else:
            key = fixture_key(
                str(request.get("method", "GET")),
                str(request.get("path", "/")),
                request.get("query"),
                request.get("body"),
            )
        out[key] = record
    return out


class ReplayTransport:
    """Answers from recorded fixtures only. Never touches the network.

    Strict by design: an unrecorded path is a test failure naming the request, which
    is how a change to the endpoints the catalog uses gets caught in review rather
    than at 3am against the live API.

    A fixture whose key names no query - ``GET /users/me/dataTypes/sleep/dataPoints`` -
    answers *any* request to that endpoint, and is only consulted when the exact request
    (path and query together) is not recorded. That is what makes a hand-written corpus
    practical: v4 names the period in the query, so "this data type answers 403" is a
    statement about an endpoint, not about one date.
    """

    #: Nothing here reaches a host, so the retired-source guard does not apply to it.
    live = False

    def __init__(self, fixtures: Path | Mapping[str, dict], *, base_url: str = "https://health.googleapis.com/v4") -> None:
        self.base_url = base_url.rstrip("/")
        self.fixtures = load_fixtures(Path(fixtures)) if not isinstance(fixtures, Mapping) else dict(fixtures)
        self.by_endpoint = {key.split("?", 1)[0]: record for key, record in self.fixtures.items() if "?" not in key}
        self.calls: list[tuple[str, str]] = []
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self.fixtures)

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        path, query = _split_url(url, self.base_url)
        key = fixture_key(method, path, query, body)
        self.calls.append((method.upper(), key))
        record = self.fixtures.get(key) or self.by_endpoint.get(f"{method.upper()} {path}")
        if record is None:
            self.misses += 1
            raise ReplayMissError(
                f"no recorded fixture for {key}",
            )
        response = record.get("response") or {}
        status = int(response.get("status", 200))
        headers_out = {str(k): str(v) for k, v in (response.get("headers") or {}).items()}
        raw = response.get("body", {})
        payload = raw if isinstance(raw, (str, bytes)) else json.dumps(raw)
        etag = headers_out.get("ETag") or headers_out.get("etag")
        if etag and (headers or {}).get("If-None-Match") == etag:
            self.hits += 1
            return Response(304, b"", {"ETag": etag})
        self.hits += 1
        return Response(status, str(payload).encode("utf-8"), headers_out)


class RecordingTransport:
    """Wraps a transport and appends everything it sees to a fixture directory."""

    def __init__(self, inner: Any, directory: Path, *, indent: int | None = 2) -> None:
        self.inner = inner
        self.directory = Path(directory)
        self.indent = indent
        self.directory.mkdir(parents=True, exist_ok=True)
        self.recorded: list[str] = []

    @property
    def live(self) -> bool:
        """Recording over the real API is live; recording over a stand-in is not."""
        return bool(getattr(self.inner, "live", False))

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        response = self.inner.request(method, url, headers, body)
        path, query = _split_url(url, "")
        key = fixture_key(method, path, query, body)
        raw_body: Any = response.body.decode("utf-8", errors="replace")
        try:
            raw_body = json.loads(raw_body)
        except ValueError:
            pass
        recorded_body: Any = None
        if body:
            try:
                recorded_body = json.loads(bytes(body).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                recorded_body = None
        record = {
            "request": {
                "method": method.upper(),
                "path": path,
                "query": dict(query or {}),
                "body": recorded_body,
            },
            "response": {
                "status": response.status,
                "headers": dict(response.headers),
                "body": raw_body,
            },
        }
        (self.directory / fixture_filename(key)).write_text(
            json.dumps(record, indent=self.indent, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.recorded.append(key)
        return response


class UrllibTransport:
    """The real transport: stdlib ``urllib``, no third-party HTTP library."""

    #: The only transport in this module that can reach a host on the internet, which is
    #: what makes it the one the retired-source guard binds.
    live = True


    def __init__(self, *, timeout: float = 30.0, opener: Any = None) -> None:
        self.timeout = timeout
        self._opener = opener

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Response:
        request = urllib.request.Request(url, data=body, method=method.upper())
        request.add_header("User-Agent", USER_AGENT)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        opener = self._opener or urllib.request.build_opener()
        try:
            with opener.open(request, timeout=self.timeout) as handle:
                return Response(int(handle.status), handle.read(), dict(handle.headers))
        except urllib.error.HTTPError as exc:  # a status is an answer, not a crash
            payload = exc.read()
            return Response(int(exc.code), payload, dict(exc.headers or {}))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise APIUnavailableError(f"cannot reach {url}: {exc}") from exc


def _split_url(url: str, base_url: str) -> tuple[str, dict[str, str]]:
    """``(path, query)`` for a URL, with the base URL, the API version and the user id
    all normalised away."""
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    if base_url:
        prefix = urllib.parse.urlsplit(base_url).path.rstrip("/")
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :] or "/"
    # The version belongs to the host and the base URL, not to the resource: a fixture is
    # named for the thing it answers, and a recording made against
    # ``https://health.googleapis.com/v4`` has to replay for a client that was told the
    # base URL in either piece (the recorder does not always know which part was config).
    if path.startswith("/v4/") or path == "/v4":
        path = path[len("/v4") :] or "/"
    # The concrete user id is an implementation detail of the fixture set: `me` is how
    # the catalog addresses the authenticated account, and a recording made with the
    # numeric ``healthUserId`` has to replay against a different account's fixtures.
    segments = path.split("/")
    if len(segments) > 2 and segments[1] == "users" and segments[2] not in ("", "me"):
        segments[2] = "me"
        path = "/".join(segments)
    # The legacy shape is still normalised, so a corpus recorded before the port - the
    # one a user who imported their history in 2025 would have - keeps replaying.
    if path.startswith("/1/user/") or path.startswith("/api/1/user/"):
        if len(segments) > 3 and segments[3] not in ("", "-"):
            segments[3] = "-"
            path = "/".join(segments)
    return path, dict(urllib.parse.parse_qsl(parsed.query))


__all__ = [
    "Response",
    "RecordingTransport",
    "ReplayTransport",
    "UrllibTransport",
    "body_signature",
    "fixture_filename",
    "fixture_key",
    "load_fixtures",
]
