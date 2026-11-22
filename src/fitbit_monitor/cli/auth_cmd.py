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
from ..api.catalog import ALL_SCOPES
from ..api.errors import CredentialsExpiredError
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
    flow = PkceFlow.new()
    url = flow.authorize_url(
        client_id=api.client_id, redirect_uri=api.redirect_uri, scopes=scopes, authorize_url=api.authorize_url
    )
    print("1. open this URL in a browser and approve the request:")
    print(f"   {url}")
    code = _collect_code(args, url, api)
    client = session.api_client()
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
    print(f"   user {tokens.user_id}, scopes: {', '.join(tokens.scopes) or 'none reported'}")
    print(f"   access token valid until {tokens.expires_at.isoformat(timespec='seconds')}")
    print("\nnext: fm import --dry-run")
    return EXIT_OK


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
    payload: dict[str, object] = {"path": str(store.path), "authenticated": tokens is not None}
    if tokens is None:
        payload["hint"] = "run `fm auth login`"
        if args.json:
            print_json(payload)
            return EXIT_OK
        print(f"not authenticated: no credentials at {store.path}")
        print("  run `fm auth login` (or `fm config init` first, if you have not registered the app)")
        return EXIT_OK
    payload.update(tokens.public())
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
    print(f"  scopes:  {', '.join(tokens.scopes) or '-'}")
    print(f"  expires: {tokens.expires_at.isoformat(timespec='seconds')} ({_human(tokens.seconds_left())} left)")
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


def _human(seconds: float) -> str:
    if seconds <= 0:
        return "expired"
    return str(timedelta(seconds=int(seconds)))


__all__ = ["cmd_auth_login", "cmd_auth_logout", "cmd_auth_status", "register"]
