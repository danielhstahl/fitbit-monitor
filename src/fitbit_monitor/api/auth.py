"""OAuth2 + PKCE against Google, and the file the tokens live in.

Google's desktop-app flow is authorization-code + PKCE with a loopback redirect: the
browser does the consent at ``accounts.google.com``, the code comes back to
``http://localhost:<port>/``, and the token exchange is a plain form POST to
``oauth2.googleapis.com/token``. Nothing here needs a public URL, a third-party service,
or a browser on a machine that also has the store - which is the only reason a
loopback redirect is allowed at all ("For testing, you can specify URIs that refer to the
local machine, such as ``http://localhost:8080``").

Three rules this module holds to:

* the token file is written ``0600`` and contains nothing but the tokens and the
  scopes they carry (see :mod:`fitbit_monitor.paths`);
* the *client* secret comes from ``config.toml`` and is never written anywhere by
  this program;
* every error a user has to act on names ``fm auth login`` in its hint.

Two ways Google's OAuth differs from the Fitbit one this code replaced, and both are the
kind that only bites at 3am:

* **A refresh token is not returned forever.** Google hands one out when consent is first
  given (or when ``prompt=consent`` forces the screen) and does *not* rotate it on a
  refresh: a refresh response normally carries only an access token. Overwriting the
  stored refresh token with the empty string the response did not send would therefore
  lock the user out an hour later, so :func:`refresh` carries the old one forward.
* **Access tokens expire in about an hour, and a withheld consent looks like a bad
  grant.** ``invalid_grant`` means revoked, expired, or - on an OAuth client in Testing
  status - past Google's seven-day authorisation expiry, and the only fix for all three
  is ``fm auth login``; ``invalid_client`` means the id and secret in ``config.toml`` do
  not match, which no login can fix. They are separate messages for that reason.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import stat
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import logsecrets
from ..errors import UsageError
from ..timeutil import ensure_utc, utcnow
from .catalog import SCOPE_PREFIX
from .errors import CredentialsExpiredError, NotAuthenticatedError
from .http import Response

REDIRECT_PATH = "/"
REFRESH_SKEW = timedelta(minutes=5)

#: Google's authorize and token endpoints. Named here so a test, a docs check and a
#: headless ``--manual`` run all agree on what the default is.
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: OAuth ``error`` codes that mean "this consent is dead" - revoked, expired, or seven
#: days old on a Testing-status client - as opposed to "you typed the wrong client id".
RECONSENT_ERRORS = frozenset({"invalid_grant", "expired_token", "invalid_token"})
CLIENT_ERRORS = frozenset({"invalid_client", "unauthorized_client", "invalid_client_id"})


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    """S256 PKCE code challenge for ``verifier`` (RFC 7636 appendix A)."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass(frozen=True)
class PkceFlow:
    """One authorization attempt: the verifier, its challenge, and the CSRF state."""

    code_verifier: str
    state: str

    @classmethod
    def new(cls) -> "PkceFlow":
        return cls(code_verifier=secrets.token_urlsafe(48), state=secrets.token_urlsafe(24))

    @property
    def code_challenge(self) -> str:
        return pkce_challenge(self.code_verifier)

    def authorize_url(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        scopes: tuple[str, ...],
        authorize_url: str = GOOGLE_AUTHORIZE_URL,
        consent: bool = False,
    ) -> str:
        """The URL to open in a browser. ``scope`` is space-delimited, per the spec.

        ``access_type=offline`` is what buys a refresh token at all - without it Google
        issues an access token and nothing to renew it with, which an import would only
        discover an hour in.

        ``consent=True`` adds ``prompt=consent``, which re-shows the consent screen and
        is the documented way to pick up changed scopes. It is deliberately opt-in: sent
        on every login it means a user who simply runs ``fm auth login`` gets a screen
        Google tells them to be suspicious of, so the caller sets it when the scopes on
        the stored token are not the scopes being asked for now.
        """
        if not scopes:
            raise UsageError("no scopes requested", hint="set [api] scopes in config.toml")
        # Last line of defence. ApiSettings resolves scope names on load, but this is the
        # one call that puts them in front of Google, and Google's answer to a scope it
        # does not know is a 400 page that says nothing a user can act on.
        not_google = [s for s in scopes if not s.startswith(SCOPE_PREFIX)]
        if not_google:
            raise UsageError(
                f"refusing to ask Google for {', '.join(not_google)}: not Google Health scopes",
                hint="fix [api] scopes in config.toml; 'fm config init --force' writes the current ones",
            )
        fields = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": self.state,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "include_granted_scopes": "true",
        }
        if consent:
            fields["prompt"] = "consent"
        return f"{authorize_url}?{urllib.parse.urlencode(fields)}"


@dataclass(frozen=True)
class TokenSet:
    """An access token, its refresh token, and what they let this program do.

    Both tokens register themselves with :mod:`~fitbit_monitor.logsecrets` the moment a
    ``TokenSet`` exists, because that is earlier than any log line that could name them:
    a token read from the credential file, returned by the token endpoint, or handed in
    by a test is masked in the log from the instant it is in this process.
    """

    access_token: str
    refresh_token: str
    expires_at: datetime
    scopes: tuple[str, ...] = ()
    token_type: str = "Bearer"
    user_id: str = "-"
    obtained_at: datetime = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        logsecrets.remember_all((self.access_token, self.refresh_token))

    def seconds_left(self, now: datetime | None = None) -> float:
        return (self.expires_at - ensure_utc(now or utcnow(), field="now")).total_seconds()

    def needs_refresh(self, now: datetime | None = None) -> bool:
        """True when the token is expired, or about to be within the skew window."""
        return self.seconds_left(now) <= REFRESH_SKEW.total_seconds()

    def authorization_header(self) -> dict[str, str]:
        return {"Authorization": f"{self.token_type} {self.access_token}"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at.isoformat(),
            "obtained_at": self.obtained_at.isoformat(),
            "scopes": list(self.scopes),
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TokenSet":
        missing = [key for key in ("access_token", "expires_at") if not raw.get(key)]
        if missing:
            raise NotAuthenticatedError(
                f"credential file is missing {', '.join(missing)}",
                hint="run `fm auth login`",
            )
        return cls(
            access_token=str(raw["access_token"]),
            refresh_token=str(raw["refresh_token"]),
            expires_at=datetime.fromisoformat(str(raw["expires_at"])),
            scopes=tuple(str(s) for s in raw.get("scopes") or ()),
            token_type=str(raw.get("token_type") or "Bearer"),
            user_id=str(raw.get("user_id") or "-"),
            obtained_at=datetime.fromisoformat(str(raw["obtained_at"])) if raw.get("obtained_at") else utcnow(),
        )

    def public(self) -> dict[str, Any]:
        """The status view: everything useful, no secrets."""
        return {
            "user_id": self.user_id,
            "scopes": list(self.scopes),
            "token_type": self.token_type,
            "expires_at": self.expires_at.isoformat(),
            "seconds_left": round(self.seconds_left()),
            "expired": self.seconds_left() <= 0,
            "needs_refresh": self.needs_refresh(),
        }


class CredentialStore:
    """The token file: ``0600``, atomic replace, and never inside the database.

    The store is a copyable, prunable artifact; the tokens are not. Keeping them in
    a separate file is what makes ``fm store export`` and "email me a backup" safe.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TokenSet:
        if not self.path.exists():
            raise NotAuthenticatedError(f"no credentials at {self.path}")
        self.reject_loose_permissions()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise NotAuthenticatedError(f"cannot read credentials at {self.path}: {exc}") from exc
        return TokenSet.from_dict(raw)

    def loose_permissions(self) -> int | None:
        """The group/other bits set on the token file, or None when it is private.

        A refresh token readable by anyone on the machine is the whole account -
        steps, sleep, heart rate and the identity Fitbit ties them to - so the mode is
        checked rather than hoped for.
        """
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
        except OSError:
            return None
        return mode if mode & 0o077 else None

    def reject_loose_permissions(self) -> None:
        mode = self.loose_permissions()
        if mode is not None:
            raise UsageError(
                f"{self.path} is readable by more than you (mode {mode:03o})",
                hint=f"run chmod 600 {self.path} - fm refuses to spend a token file others can read",
            )

    def load_or_none(self) -> TokenSet | None:
        try:
            return self.load()
        except NotAuthenticatedError:
            return None

    def save(self, tokens: TokenSet) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(tokens.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)
        os.chmod(self.path, 0o600)
        return self.path

    def delete(self) -> bool:
        if not self.path.exists():
            return False
        tokens = self.load_or_none()
        if tokens is not None:  # stop masking what no longer exists
            logsecrets.forget(tokens.access_token)
            logsecrets.forget(tokens.refresh_token)
        self.path.unlink()
        return True


def _basic_auth(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")


def _post_token(
    transport: Any,
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    fields: Mapping[str, str],
    now: datetime,
    keep_refresh_token: str = "",
) -> TokenSet:
    body = urllib.parse.urlencode(fields).encode("ascii")
    response: Response = transport.request(
        "POST",
        token_url,
        {
            "Authorization": _basic_auth(client_id, client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        body,
    )
    if not response.ok:
        raise _token_error(response)
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise CredentialsExpiredError("the token endpoint returned no access token")
    expires_in = float(payload.get("expires_in") or 28800)
    scopes = payload.get("scope") or ""
    issued = str(payload.get("refresh_token") or "") or keep_refresh_token
    if issued:
        logsecrets.remember(issued)
    return TokenSet(
        access_token=str(payload["access_token"]),
        refresh_token=issued,
        expires_at=ensure_utc(now) + timedelta(seconds=expires_in),
        scopes=tuple(str(s) for s in (scopes.split() if isinstance(scopes, str) else list(scopes))),
        token_type=str(payload.get("token_type") or "Bearer"),
        user_id=str(payload.get("user_id") or "-"),
        obtained_at=ensure_utc(now),
    )


def _token_error(response: Response) -> CredentialsExpiredError:
    """A refusal from the token endpoint, in the dialect that says what to do next.

    Google's OAuth errors are ``{"error": "invalid_grant", "error_description": ...}``,
    where the legacy endpoint used ``{"errors": [{"errorType": ...}]}``; both are read,
    and the distinction that matters is *whose* problem it is. ``invalid_grant`` is the
    user's and one command fixes it; ``invalid_client`` is the config's, and telling
    somebody to ``fm auth login`` against a client id that does not exist is the same
    loop the retired-host guard exists to break.
    """
    code, description = _oauth_error(response)
    if code in CLIENT_ERRORS:
        return CredentialsExpiredError(
            f"the OAuth client was refused: {code}{' ' + description if description else ''}".strip(),
        )
    if code in RECONSENT_ERRORS:
        return CredentialsExpiredError(
            f"the stored consent is no longer valid: {code}{' ' + description if description else ''}".strip()
        )
    detail = description or code or _error_detail(response)
    return CredentialsExpiredError(f"the token endpoint refused the request: HTTP {response.status} {detail}".strip())


def _oauth_error(response: Response) -> tuple[str | None, str]:
    """``(error, error_description)`` from either dialect, or ``(None, "")``."""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a body we cannot parse is not worth a traceback
        return None, response.text[:200]
    if not isinstance(payload, dict):
        return None, ""
    code = payload.get("error") or payload.get("errorType")
    description = str(payload.get("error_description") or payload.get("message") or "")
    if not code and payload.get("errors"):
        first = payload["errors"][0]
        if isinstance(first, dict):
            code, description = first.get("errorType"), str(first.get("message") or description)
    return (str(code) if code else None), description[:200]


def _error_detail(response: Response) -> str:
    code, description = _oauth_error(response)
    if code:
        return f"{code} {description}".strip()[:200]
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a body we cannot parse is not worth a traceback
        return response.text[:200]
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if errors:
            return json.dumps(errors)[:200]
        return str(payload)[:200]
    return ""


def exchange_code(
    transport: Any,
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    now: datetime | None = None,
) -> TokenSet:
    """Swap an authorization code (and the PKCE verifier) for tokens."""
    return _post_token(
        transport,
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        fields={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        now=now or utcnow(),
    )


def refresh(
    transport: Any,
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    now: datetime | None = None,
) -> TokenSet:
    """Refresh an access token.

    Google does not rotate refresh tokens the way the legacy API did: a refresh reply
    usually carries only an access token, so the stored refresh token is carried forward
    when the response does not name a new one. Writing the empty string there would look
    like a successful refresh and leave nothing to renew with an hour later - a renewal
    that fails quietly is the one failure an import cannot recover from by itself.
    """
    if not refresh_token:
        raise CredentialsExpiredError("the stored credentials have no refresh token")
    return _post_token(
        transport,
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        fields={"grant_type": "refresh_token", "refresh_token": refresh_token},
        now=now or utcnow(),
        keep_refresh_token=refresh_token,
    )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures ``?code=`` (or ``?error=``) once, then hands the browser a stub page."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlsplit(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.server.params = params  # type: ignore[attr-defined]
        body = (
            b"<html><body><h2>fitbit-monitor</h2><p>Captured the authorization "
            b"response - you can close this tab and return to the terminal.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence the stdlib's stderr chatter
        return


class LoopbackCallback:
    """A one-shot localhost HTTP server that waits for the redirect.

    ``port`` should match ``[api] redirect_port``, because Fitbit compares the
    redirect URI exactly; pass 0 in tests to let the OS pick one.
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, timeout: float = 300.0) -> None:
        self.host = host
        self.requested_port = port
        self.timeout: float = timeout
        self.port = port

    def redirect_uri(self, host: str = "localhost") -> str:
        return f"http://{host}:{self.port}{REDIRECT_PATH}"

    def wait(self) -> dict[str, str]:
        """Block until the redirect lands. Raises :class:`UsageError` on timeout/error."""
        server = _CallbackWait((self.host, self.requested_port), _CallbackHandler, timeout=self.timeout)
        self.port = server.server_address[1]
        try:
            params = server.wait_for_params()
        finally:
            server.shutdown_server()
        if "error" in params:
            raise UsageError(
                f"the authorization was refused: {params['error']}",
                hint=f"check the redirect URI registered for the app is exactly {self.redirect_uri()}",
            )
        if "code" not in params:
            raise UsageError("the authorization redirect carried no code")
        return params


class _CallbackWait(http.server.ThreadingHTTPServer):
    """A HTTPServer that serves until one request has landed (or the clock runs out)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, timeout: float) -> None:  # type: ignore[no-untyped-def]
        super().__init__(address, handler)
        self.params: dict[str, str] | None = None
        self.timeout: float = timeout
        self._done = threading.Event()
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def wait_for_params(self) -> dict[str, str]:
        """Block for the redirect: the request lands, or ``timeout`` seconds pass."""
        waited = 0.0
        while self.params is None and waited < self.timeout:
            self._done.wait(0.02)
            waited += 0.02
        if self.params is None:
            raise UsageError(
                "timed out waiting for the browser redirect",
                hint="for a machine with no browser, use `fm auth login --manual <code>`",
            )
        return self.params

    def shutdown_server(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)

    def finish_request(self, request, client_address) -> None:  # type: ignore[no-untyped-def]
        super().finish_request(request, client_address)
        self._done.set()


def fetch_redirect_manually(prompt: str, input_fn: Callable[[str], str] = input) -> str:
    """The no-browser path: paste the ``code`` from the redirect URL by hand."""
    raw = input_fn(prompt).strip()
    if not raw:
        raise UsageError("no authorization code entered")
    if "code=" in raw:
        parsed = urllib.parse.urlsplit(raw)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if "code" in params:
            return params["code"]
        raise UsageError("that URL has no code= parameter", hint="paste the whole redirect URL, or just the code")
    return raw


def open_browser(url: str) -> bool:
    """Best-effort browser open; a headless box is normal here, so failure is quiet."""
    import webbrowser

    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - no browser, no DISPLAY, no XDG session
        return False


__all__ = [
    "CredentialStore",
    "LoopbackCallback",
    "PkceFlow",
    "TokenSet",
    "exchange_code",
    "fetch_redirect_manually",
    "open_browser",
    "pkce_challenge",
    "refresh",
]
