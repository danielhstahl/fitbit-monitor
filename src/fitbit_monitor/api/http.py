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


def fixture_key(method: str, path: str, query: Mapping[str, Any] | None = None) -> str:
    """The name of the fixture for a request, independent of query ordering."""
    normalized = path if path.startswith("/") else f"/{path}"
    key = f"{method.upper()} {normalized}"
    if query:
        encoded = urllib.parse.urlencode(sorted((k, str(v)) for k, v in query.items()))
        if encoded:
            key = f"{key}?{encoded}"
    return key


def fixture_filename(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "_", key).strip("_") + ".json"


def load_fixtures(directory: Path) -> dict[str, dict]:
    """Every fixture in ``directory``, keyed by :func:`fixture_key`."""
    out: dict[str, dict] = {}
    for path in sorted(Path(directory).rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ReplayMissError(f"{path}: not valid fixture JSON ({exc})") from exc
        request = record.get("request") or {}
        if "key" in record:  # hand-written fixtures may name themselves
            key = str(record["key"])
        else:
            key = fixture_key(str(request.get("method", "GET")), str(request.get("path", "/")), request.get("query"))
        out[key] = record
    return out


class ReplayTransport:
    """Answers from recorded fixtures only. Never touches the network.

    Strict by design: an unrecorded path is a test failure naming the request, which
    is how a change to the endpoints the catalog uses gets caught in review rather
    than at 3am against the live API.
    """

    #: Nothing here reaches a host, so the retired-source guard does not apply to it.
    live = False

    def __init__(self, fixtures: Path | Mapping[str, dict], *, base_url: str = "https://api.fitbit.com") -> None:
        self.base_url = base_url.rstrip("/")
        self.fixtures = load_fixtures(Path(fixtures)) if not isinstance(fixtures, Mapping) else dict(fixtures)
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
        key = fixture_key(method, path, query)
        self.calls.append((method.upper(), key))
        record = self.fixtures.get(key)
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
        key = fixture_key(method, path, query)
        raw_body: Any = response.body.decode("utf-8", errors="replace")
        try:
            raw_body = json.loads(raw_body)
        except ValueError:
            pass
        record = {
            "request": {"method": method.upper(), "path": path, "query": dict(query or {})},
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
    """``(path, query)`` for a URL, with the base URL and the user id normalised away."""
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    if base_url:
        prefix = urllib.parse.urlsplit(base_url).path.rstrip("/")
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :] or "/"
    # The concrete user id is an implementation detail of the fixture set: "-" is the
    # Fitbit id for "the authenticated user", so fixtures are written in that form.
    if path.startswith("/1/user/") or path.startswith("/api/1/user/"):
        segments = path.split("/")
        if len(segments) > 3 and segments[3] not in ("", "-"):
            segments[3] = "-"
            path = "/".join(segments)
    return path, dict(urllib.parse.parse_qsl(parsed.query))


__all__ = [
    "Response",
    "RecordingTransport",
    "ReplayTransport",
    "UrllibTransport",
    "fixture_filename",
    "fixture_key",
    "load_fixtures",
]
