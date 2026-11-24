"""``fm auth`` - the OAuth2 PKCE login, its status, and its removal.

    fm auth login              open the consent page, catch the redirect, store the token
    fm auth login --manual     same, for a machine with no browser (paste the code)
    fm auth status             what is stored, what it grants, when it expires
    fm auth logout             delete the token file

Nothing here needs a public URL: Fitbit's desktop-app flow redirects to
``http://localhost:<port>/`` and :class:`~fitbit_monitor.api.auth.LoopbackCallback`
catches it once. The token file is ``0600`` and lives in the data dir, *not* in the
store, so a store backup is not a credential leak.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from ..api.auth import LoopbackCallback, PkceFlow, exchange_code, fetch_redirect_manually, open_browser
from ..api.catalog import ALL_SCOPES, short_scope
from ..api.auth import TokenSet
from ..api.errors import CredentialsExpiredError
from ..api.source import LEGACY_SUNSET, TOKEN_PLANE_SETTINGS, guard as guard_endpoints, state_of
from ..errors import UsageError
from ..timeutil import utcnow
from .common import EXIT_ERROR, EXIT_OK, Session, print_json
from .config_cmd import require_api_config


def register(sub: "argparse._SubParsersAction") -> None:
    auth = sub.add_parser("auth", help="authorize fm against your Fitbit account")
    asub = auth.add_subparsers(dest="auth_command", required=True)

    login = asub.add_parser("login", help="authorize and store the tokens")
    login.add_argument("--manual", nargs="?", const="", default=None, metavar="CODE",
                       help="no browser: paste the ?code= from the redirect URL")
    login.add_argument("--no-browser", action="store_true", help="print the URL instead of opening a browser")
    login.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the redirect (default 300)")
    login.add_argument("--json", action="store_true")
    login.set_defaults(func=cmd_auth_login)

    status = asub.add_parser("status", help="what is stored, and what it grants")
    status.add_argument("--probe", action="store_true", help="also call the API (costs one request)")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_auth_status)

    logout = asub.add_parser("logout", help="delete the stored tokens")
    logout.add_argument("--json", action="store_true")
    logout.set_defaults(func=cmd_auth_logout)


def cmd_auth_login(args: argparse.Namespace, session: Session) -> int:
    api = require_api_config(session)
    scopes = tuple(api.scopes) or ALL_SCOPES
    client = session.api_client()
    # Refuse a retired token endpoint before the browser opens, not after the code comes
    # back: the consent page still renders, so a user who gets as far as "approve" is
    # then told their own approval failed. Only the token endpoint is checked here - the
    # browser dials the consent URL, and the note below says what a retired one means.
    guard_endpoints(api, live=client.live, settings=TOKEN_PLANE_SETTINGS)
    # If the consent URL is the retired one, say so before sending the user to approve
    # there: the page still renders, so the failure would otherwise arrive as their fault.
    note = _source_note(api, setting="authorize_url")
    if note:
        print(note)
    flow = PkceFlow.new()
    url = flow.authorize_url(
        client_id=api.client_id, redirect_uri=api.redirect_uri, scopes=scopes, authorize_url=api.authorize_url
    )
    print("1. open this URL in a browser and approve the request:")
    print(f"   {url}")
    code = _collect_code(args, url, api)
    try:
        tokens = exchange_code(
            client.transport,
            token_url=api.token_url,
            client_id=api.client_id,
            client_secret=api.client_secret,
            code=code,
            redirect_uri=api.redirect_uri,
            code_verifier=flow.code_verifier,
            now=utcnow(),
        )
    except CredentialsExpiredError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    path = session.credentials().save(tokens)
    if args.json:
        print_json({"status": "ok", "credentials": str(path), **tokens.public()})
        return EXIT_OK
    print(f"2. stored tokens in {path} (mode 0600)")
    print(f"   user {tokens.user_id}, scopes: {_scopes(tokens)}")
    print(f"   access token valid until {tokens.expires_at.isoformat(timespec='seconds')}")
    print("\nnext: fm import --dry-run")
    return EXIT_OK


def _source_note(api, *, setting: str | None = None) -> str | None:
    """A warning line when an ``[api]`` endpoint belongs to the retired source, else nothing.

    Phrased as a note rather than a refusal because the token *machinery* is not broken -
    PKCE, storage and refresh all work, and work against fixtures or any other host -
    while the Fitbit endpoints named in the default config issue no tokens at all. Pass
    ``setting`` to talk about one endpoint specifically (a login command cares about the
    consent page it is about to show, and nothing else).
    """
    state = state_of(api) if setting is None else state_of(api, settings=(setting,))
    if not state.is_retired:
        return None
    names = ", ".join(f"[api] {name}" for name, _url in state.retired)
    return (
        f"note: {names} is the retired Fitbit Web API, switched off {LEGACY_SUNSET}: it still "
        "answers, but it issues no tokens and no data, so nothing started here can finish "
        "until the port in docs/adr/0002-health-api-source.md section 10 lands. `fm api source "
        "--verify` shows what does still answer; `fm import --source csv` works today."
    )


def _collect_code(args: argparse.Namespace, url: str, api) -> str:
    manual = args.manual
    if manual is not None:
        code = manual or fetch_redirect_manually("2. paste the code from the redirect URL (or the whole URL): ")
        return code
    if not args.no_browser:
        if not open_browser(url):
            print("   (no browser opened - copy the URL above into one, or re-run with --manual)")
    print(f"2. waiting for the redirect on {api.redirect_uri} (Ctrl-C to cancel)")
    callback = LoopbackCallback(host="127.0.0.1", port=api.redirect_port, timeout=args.timeout)
    params = callback.wait()
    if params.get("state") != _state_of(url):
        raise UsageError("the redirect carried an unexpected state", hint="re-run `fm auth login` and retry")
    return params["code"]


def _state_of(url: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return (parse_qs(urlsplit(url).query) or {}).get("state", [""])[0]


def cmd_auth_status(args: argparse.Namespace, session: Session) -> int:
    store = session.credentials()
    tokens = store.load_or_none()
    note = _source_note(session.config.api)
    payload: dict[str, object] = {"path": str(store.path), "authenticated": tokens is not None}
    if tokens is None:
        payload["hint"] = "run `fm auth login`"
        if note:
            payload["warning"] = note.replace("note: ", "", 1)
        if args.json:
            print_json(payload)
            return EXIT_OK
        print(f"not authenticated: no credentials at {store.path}")
        print("  run `fm auth login` (or `fm config init` first, if you have not registered the app)")
        if note:
            print(f"  {note}")
        return EXIT_OK
    payload.update(tokens.public())
    if note:
        payload["warning"] = note.replace("note: ", "", 1)
    extra: dict[str, object] = {}
    if args.probe:
        client = session.api_client()
        client.set_tokens(tokens)
        try:
            profile = client.profile()
            user = profile.get("user", profile)
            extra = {
                "probe": {"user_id": user.get("memberId") or user.get("displayName"), "resources": user.get("resources", [])},
                "rate_limit": client.rate_limit.as_dict(),
            }
            payload.update(extra)
        except Exception as exc:  # noqa: BLE001 - a probe failure is a reportable status, not a crash
            payload["probe_error"] = str(exc)
    if args.json:
        print_json(payload)
        return EXIT_OK
    print(f"authenticated as {tokens.user_id} (credentials: {store.path})")
    print(f"  scopes:  {_scopes(tokens)}")
    print(f"  expires: {tokens.expires_at.isoformat(timespec='seconds')} ({_human(tokens.seconds_left())} left)")
    if note:
        print(f"  {note}")
    if tokens.needs_refresh():
        print("  the access token is expired or nearly so; the next import refreshes it automatically")
    if args.probe and "probe" in payload:
        probe = payload["probe"]  # type: ignore[assignment]
        print(f"  profile: user {probe['user_id']}, resources {', '.join(probe['resources']) or '-'}")  # type: ignore[index]
    return EXIT_OK


def cmd_auth_logout(args: argparse.Namespace, session: Session) -> int:
    store = session.credentials()
    removed = store.delete()
    if args.json:
        print_json({"removed": removed, "path": str(store.path)})
        return EXIT_OK
    print(f"removed {store.path}" if removed else f"nothing to remove at {store.path}")
    return EXIT_OK


def _scopes(tokens: TokenSet) -> str:
    """Granted scopes as short names.

    The v4 scope URLs are ~70 characters apiece and differ only in the middle word, so
    the human line prints what differs; ``auth status --json`` still carries the URLs
    verbatim.
    """
    return ", ".join(short_scope(scope) for scope in tokens.scopes) or "none reported"


def _human(seconds: float) -> str:
    if seconds <= 0:
        return "expired"
    return str(timedelta(seconds=int(seconds)))


__all__ = ["cmd_auth_login", "cmd_auth_logout", "cmd_auth_status", "register"]
