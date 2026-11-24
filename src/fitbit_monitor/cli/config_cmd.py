"""``fm config`` - the starter file, and what fm actually resolved.

    fm config init            write a commented config.toml to the config path
    fm config show            the resolved paths, API settings and import settings
    fm config path            just the paths, one per line (shell-friendly)

``init`` exists because the first-run path has real friction: you have to register a
Fitbit app, paste its client id, and the **redirect URI must match the registered one
character for character**. Printing the exact URI to paste is most of the support
burden of this project, so it is printed by the command instead of being written down
in a doc someone skims.
"""

from __future__ import annotations

import argparse
import os

from ..api.catalog import ALL_SCOPES
from ..api.source import WORKING_TODAY, state_of
from ..config import ApiSettings
from ..errors import UsageError
from pathlib import Path
from .common import EXIT_OK, Session, print_json

STARTER = """\
# fitbit-monitor configuration. All of it is optional; `fm config show` prints what
# is in force after defaults, environment variables and flags are applied.

[store]
path = "{store}"

# THE ONE PART YOU BRING YOURSELF: AN OAUTH CLIENT.
# The source is the Google Health API (docs/adr/0002-health-api-source.md) and `fm`
# speaks it - health.googleapis.com/v4, the four googlehealth.* readonly scopes below.
# It is Personal mode, so there is no approval queue and no fee, and no client is shipped
# in this repository either: a personal tool with an embedded client secret is a hosted
# service pretending not to be one. To get a token:
#   1. a Google Cloud project with the Google Health API enabled, in Personal mode
#   2. an OAuth client of that project
#   3. its id below, and this redirect URI registered exactly - scheme, host, port and
#      trailing slash all have to match (a loopback URI is allowed for a local tool):
#   {redirect}
# A Testing-status client expires consent after seven days; `fm auth login` re-asks for
# consent when it has to, and says so rather than looking like a bad password.
#
# The legacy Fitbit Web API was switched off on 2026-11-02. A base_url naming it is
# refused with exit 6 rather than dialled, because it answers 401 and "log in again" is
# advice with no exit. `fm api source` prints all of that in one line.
#
# And the other source needs no client, no consent and no network at all:
#   fm import --source csv --path ~/Downloads/takeout.zip
[api]
client_id = ""
client_secret = ""
redirect_port = {port}
scopes = [{scopes}]

# How an ad-hoc import behaves. Every key here has a default; they are listed so the
# knobs are visible - see docs/data-import.md. `timezone` is the account timezone: the
# days and the civil times the API and the export CSVs report arrive without an offset,
# and this is what says where they were spoken.
[import]
timezone = "UTC"
recent_days = 7
confirm_above_requests = 2000

[import.chunk_days]
hrv = 7

[plugins]
dirs = []

[alerts]
cooldown = "36h"
jsonl = true
console = true
"""


def register(sub: "argparse._SubParsersAction") -> None:
    config = sub.add_parser("config", help="initialise and inspect configuration")
    csub = config.add_subparsers(dest="config_command", required=True)

    init = csub.add_parser("init", help="write a starter config.toml")
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.add_argument("--print", dest="print_only", action="store_true", help="show it without writing")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_config_init)

    show = csub.add_parser("show", help="resolved paths, API and import settings")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_config_show)

    paths = csub.add_parser("path", help="print the resolved paths, one per line")
    paths.add_argument("--json", action="store_true")
    paths.set_defaults(func=cmd_config_path)


def require_api_config(session: Session) -> ApiSettings:
    """The API settings, or an error that says what to do about it."""
    api = session.config.api
    if not api.configured:
        raise UsageError(
            "no Fitbit app is configured",
            hint="run `fm config init`, paste your client_id/client_secret into [api], then `fm auth login`",
        )
    return api


def render_starter(session: Session) -> str:
    api = session.config.api
    return STARTER.format(
        store=session.store_path,
        redirect=api.redirect_uri,
        port=api.redirect_port,
        scopes=", ".join(f'"{s}"' for s in (api.scopes or ALL_SCOPES)),
    )


def cmd_config_init(args: argparse.Namespace, session: Session) -> int:
    path = session.paths.config
    text = render_starter(session)
    if args.print_only:
        print(text)
        return EXIT_OK
    if path.exists() and not args.force:
        raise UsageError(f"{path} already exists", hint="edit it, or re-run with --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    if args.json:
        print_json({"path": str(path), "redirect_uri": session.config.api.redirect_uri, "created": True})
        return EXIT_OK
    print(f"wrote {path} (mode 0600)")
    print("\nnext:")
    print("  1. with no API, no client and no network - years of history, all at once:")
    print("       fm import --source csv --path ~/Downloads/takeout.zip")
    print("  2. for the API: register an OAuth client with redirect URI")
    print(f"       {session.config.api.redirect_uri}")
    print(f"     paste its id (and secret, if it has one) into [api] in {path}, then `fm auth login`")
    print("  3. `fm api source` prints what this build dials, and refuses a config left on")
    print("     the retired Fitbit host rather than dialling it")
    return EXIT_OK


def cmd_config_show(args: argparse.Namespace, session: Session) -> int:
    config = session.config
    import_settings = config.import_
    source = state_of(config.api)
    payload = {
        "config_path": str(session.paths.config),
        "config_present": session.paths.config.exists(),
        "data_dir": str(session.paths.data_dir),
        "store": str(session.store_path),
        "store_present": session.store_path.exists(),
        "credentials": str(session.credentials().path),
        "credentials_present": session.credentials().exists,
        "imports_dir": str(session.paths.imports),
        "plugin_dirs": [str(p) for p in config.plugin_dirs],
        "plugins": sorted(config.plugin_sections),
        "api": config.api.redacted(),
        "source": source.as_dict(),
        "import": {
            "timezone": import_settings.timezone,
            "recent_days": import_settings.recent_days,
            "chunk_days": dict(import_settings.chunk_days),
            "rate_limit_reserve": import_settings.rate_limit_reserve,
            "max_wait_seconds": import_settings.max_wait_seconds,
            "confirm_above_requests": import_settings.confirm_above_requests,
            "report_keep": import_settings.report_keep,
        },
    }
    if args.json:
        print_json(payload)
        return EXIT_OK
    print(f"config:   {payload['config_path']}{'' if payload['config_present'] else ' (not created - `fm config init`)'}")
    print(f"data dir: {payload['data_dir']}")
    print(f"store:    {payload['store']}{'' if payload['store_present'] else ' (no store yet)'}")
    print(f"tokens:   {payload['credentials']}{'' if payload['credentials_present'] else ' (not authenticated)'}")
    print(f"reports:  {payload['imports_dir']}")
    api = config.api
    print(f"api:      client_id={api.client_id or '(not set)'} redirect_uri={api.redirect_uri}")
    print(f"source:   {source.short()}")
    if source.is_retired:
        print(f"          {WORKING_TODAY}")
    print(f"import:   timezone={import_settings.timezone} recent_days={import_settings.recent_days} "
          f"chunk_days={dict(import_settings.chunk_days) or '-'}")
    return EXIT_OK


def cmd_config_path(args: argparse.Namespace, session: Session) -> int:
    """The paths, plainly: the one flag you need when a store is not where you expected."""
    resolved = {
        "config": session.paths.config,
        "data_dir": session.paths.data_dir,
        "store": session.store_path,
        "credentials": session.credentials().path,
        "imports": session.paths.imports,
        "etag_cache": session.paths.etag_cache,
    }
    if args.json:
        print_json({key: str(value) for key, value in resolved.items()})
        return EXIT_OK
    for key, value in resolved.items():
        state = "" if Path(value).exists() else "  (does not exist yet)"
        print(f"{key}\t{value}{state}")
    return EXIT_OK


__all__ = ["render_starter", "require_api_config", "cmd_config_init", "cmd_config_show", "register"]
