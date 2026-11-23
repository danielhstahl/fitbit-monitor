"""``fm doctor`` - "what is wrong with this install?", in one pass.

The command a user runs *first*, when something looks off but no error message is
available: an import that landed nothing, an alert that never arrived, a machine where
fm worked last week. Every answer is a line: what was checked, the verdict, and the
few facts it rested on.

Verdicts, and the only thing that decides the exit code:

``ok``       the check passed.
``warn``     something is off but nothing is broken - a fresh install with no token, an
             expired-but-refreshable access token, a plugin that will not load.
``fail``     fm cannot do the job this check covers: a config that does not parse, a
             data directory that cannot be written, a store file that is not a store.
``skipped``  not looked at, and why (``--quick``, or a precondition was missing).

**The exit code is nonzero only for a ``fail``** - a missing token is a warning on a
laptop that has never logged in, and a diagnostic that cries wolf on a fresh install
gets ignored on a machine that is genuinely broken. ``--json`` prints the same report
as one object with a ``checks`` array, for scripts and for CI.

Human lines are shaped for ``grep``: a bracketed verdict, the check name, then
``key=value`` facts with spaces quoted out:

    [ok]      config     path=/home/u/.config/fitbit-monitor/config.toml exists=yes
    [warn]    token      path=/home/u/.local/share/fitbit-monitor/credentials.json present=no
    [fail]    data_dir   path=/srv/fm exists=yes writable=no error="Read-only file system"

The facts a check is named for are always in its line and always in its JSON object -
``token`` always answers ``present`` and ``expired``, ``store`` always answers
``schema_version`` - and say ``-``/``null`` when there is nothing to answer with, so a
consumer never has to guess whether a missing key meant "no" or "not checked". Extras
that only apply sometimes (a token's scopes, a store's pending migrations) appear when
they do.
"""

from __future__ import annotations

import argparse
import os
import platform
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import __version__
from ..api.auth import CredentialStore
from ..errors import FmError
from ..plugins.contract import PLUGIN_API_VERSION
from ..store import schema
from .common import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Session, print_json

DESCRIPTION = """\
Diagnose this install: config, data dir, credentials, store, plugins, clock, last import.

One line per check, with a verdict: ok, warn, fail or skipped. The exit code is nonzero
only when something FAILED - a fresh install with no token yet is a warning, not a
failure. --json prints the same report as one JSON object.
"""

EPILOG = """\
examples:
  fm doctor                                 the whole report
  fm doctor --json | jq '.checks[] | select(.status!="ok")'
  grep -E '^\\[(fail|warn)\\]' <(fm doctor)   just the problems
  fm doctor --quick                         skip the store scans (integrity, future rows)

see also: fm config show, fm auth status, fm db status, fm store verify, fm plugins list
"""

# --- verdicts ---------------------------------------------------------------
OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"

#: Width of the bracketed verdict column, so the names line up.
_STATUS_WIDTH = max(len(SKIPPED), len(FAIL)) + 2

#: The year range a clock reading is believed in. Outside it, every timestamp fm writes
#: or reads is wrong, which is a hard failure rather than a curiosity.
_CLOCK_MIN_YEAR = 2015
_CLOCK_MAX_YEAR = 2100

#: Timestamps further ahead of now than this are treated as a skewed clock, not as a
#: device that synced a few minutes early.
_FUTURE_TOLERANCE_SECONDS = 600.0

#: What a SQLite file starts with. Checked before opening: "that is not a database" is
#: a much better diagnosis than whatever SQLite says when handed one.
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _offset_text(offset: timedelta | None) -> str:
    """A UTC offset as ``+02:00`` - what a reader compares against their wall clock."""
    seconds = int((offset or timedelta(0)).total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, minutes = divmod(abs(seconds), 3600)
    return f"{sign}{hours:02d}:{minutes // 60:02d}"


def _fmt(value: Any) -> str:
    """One detail value, as a single grep-friendly token."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, (list, tuple, set, frozenset)):
        joined = ",".join(str(item) for item in sorted(value, key=str)) if value else "-"
        return _quote(joined)
    text = str(value)
    return _quote(text) if any(char.isspace() for char in text) or not text else text


def _quote(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def _short(error: BaseException) -> str:
    """An exception as one line, capped: a SQLite message can be a paragraph."""
    text = " ".join(str(error).split())
    return text[:160]


@dataclass
class Check:
    """One check, its verdict, and the facts the verdict rests on."""

    name: str
    status: str = OK
    detail: Mapping[str, Any] = field(default_factory=dict)
    hint: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def as_dict(self) -> dict[str, Any]:
        """The JSON view: keys always present, so a consumer can index blindly."""
        payload: dict[str, Any] = {"check": self.name, "status": self.status, "hint": self.hint}
        for key, value in self.detail.items():
            payload[key] = _jsonable(value)
        return payload

    def line(self) -> str:
        detail = " ".join(f"{key}={_fmt(value)}" for key, value in self.detail.items())
        verdict = f"[{self.status}]".ljust(_STATUS_WIDTH + 2)
        return f"{verdict}{self.name.ljust(12)} {detail}".rstrip()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass
class Context:
    """What a check is allowed to look at."""

    session: Session
    quick: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def paths(self):
        return self.session.paths

    @property
    def config(self):
        return self.session.config


# --- checks -----------------------------------------------------------------
def check_version(ctx: Context) -> Check:
    """Versions a bug report needs, in one line."""
    return Check(
        "version",
        OK,
        {
            "fm": __version__,
            "plugin_api": PLUGIN_API_VERSION,
            "schema_known": schema.SCHEMA_VERSION,
            "python": platform.python_version(),
            "platform": f"{platform.system()}-{platform.release()}",
            "sqlite": sqlite3.sqlite_version,
        },
    )


def check_config(ctx: Context) -> Check:
    """The config file: where it resolved to, and whether it is readable and private."""
    path = ctx.paths.config
    detail: dict[str, Any] = {"path": path, "exists": path.is_file()}
    if not detail["exists"]:
        detail["source"] = "defaults and environment"
        return Check("config", OK, detail, hint=f"`fm config init` writes a starter file at {path}")
    try:
        info = path.stat()
    except OSError as exc:  # vanished, or not readable: nothing after this can be trusted
        return Check("config", FAIL, {**detail, "error": _short(exc)}, hint="fix the permissions on the config path")
    detail["bytes"] = info.st_size
    detail["mode"] = f"{stat.S_IMODE(info.st_mode):03o}"
    detail["parsed"] = True  # a file that does not parse never reaches this command
    secretish = bool(ctx.config.api.client_secret or ctx.config.webhook.url)
    if info.st_mode & 0o077 and secretish:
        return Check(
            "config",
            WARN,
            {**detail, "world_readable": True},
            hint=f"chmod 600 {path} - it holds a client secret or a webhook URL",
        )
    return Check("config", OK, detail)


def _writable(directory: Path) -> tuple[bool, str]:
    """Can a file be created in ``directory``? Probed, not asked.

    ``os.access`` reports what the mode bits claim; this answers what actually happens,
    which is the difference on a read-only mount, an immutable flag or an ACL.
    """
    probe = ""
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".fm-doctor-", delete=False) as handle:
            probe = handle.name
        return True, ""
    except OSError as exc:
        return False, _short(exc)
    finally:
        if probe:
            try:
                os.unlink(probe)
            except OSError:  # pragma: no cover - the probe is a courtesy
                pass


def check_data_dir(ctx: Context) -> Check:
    """The data directory: the store, the token file and the import reports live here."""
    path = ctx.paths.data_dir
    detail: dict[str, Any] = {"path": path, "exists": path.is_dir(), "writable": False}
    if not detail["exists"]:
        # Not created yet: what matters is whether fm *could* create it, which is whether
        # the nearest existing ancestor takes a file.
        parent = path.parent
        while not parent.is_dir() and parent != parent.parent:
            parent = parent.parent
        can_write, why = _writable(parent)
        detail["writable"] = can_write
        detail["creatable"] = can_write
        detail["probe_dir"] = parent
        if why:
            detail["error"] = why
        return Check(
            "data_dir",
            OK if can_write else FAIL,
            detail,
            hint="" if can_write else f"create {path} or point --data-dir somewhere writable",
        )
    can_write, why = _writable(path)
    detail["writable"] = can_write
    if not can_write:
        return Check(
            "data_dir",
            FAIL,
            {**detail, "error": why},
            hint=f"make {path} writable - fm cannot write its store or its token file",
        )
    return Check("data_dir", OK, detail)


def check_token(ctx: Context) -> Check:
    """The OAuth token: present, private, and current."""
    store: CredentialStore = ctx.session.credentials()
    detail: dict[str, Any] = {"path": store.path, "present": store.path.is_file(), "expired": None, "needs_refresh": None}
    if not detail["present"]:
        return Check("token", WARN, detail, hint="run `fm auth login` (an import needs a token)")
    loose = store.loose_permissions()
    if loose is not None:
        return Check(
            "token",
            FAIL,
            {**detail, "mode": f"{loose:03o}", "error": "readable by group or other"},
            hint=f"chmod 600 {store.path} - fm refuses to spend a token file others can read",
        )
    try:
        tokens = store.load()
    except FmError as exc:
        return Check("token", FAIL, {**detail, "error": _short(exc)}, hint="re-run `fm auth login`")
    detail.update(
        {
            "mode": "600",
            "user_id": tokens.user_id,
            "scopes": list(tokens.scopes),
            "expires_at": tokens.expires_at,
            "seconds_left": int(tokens.seconds_left(ctx.now)),
            "expired": tokens.seconds_left(ctx.now) <= 0,
            "needs_refresh": tokens.needs_refresh(ctx.now),
        }
    )
    if tokens.needs_refresh(ctx.now):
        return Check(
            "token",
            WARN,
            detail,
            hint="the next request refreshes it; if that refresh is refused, run `fm auth login`",
        )
    return Check("token", OK, detail)


def check_store(ctx: Context) -> Check:
    """The store file: it exists, it is a store, it is not newer than this build."""
    path = ctx.session.store_path
    detail: dict[str, Any] = {"path": path, "exists": path.is_file(), "schema_version": None}
    if not detail["exists"]:
        return Check(
            "store",
            WARN,
            detail,
            hint="`fm db init` creates an empty store; `fm dev seed --days 90` fills one with sample data",
        )
    detail["bytes"] = path.stat().st_size
    if detail["bytes"] == 0:
        # A zero-length file is not corruption - `fm db init` fills it in - but there is
        # nothing to read yet, and SQLite refuses to negotiate WAL on it read-only.
        return Check(
            "store",
            WARN,
            {**detail, "error": "empty file"},
            hint=f"nothing in that file yet: `fm db init` writes the schema into {path}",
        )
    with path.open("rb") as handle:
        magic = handle.read(16)
    if magic != _SQLITE_MAGIC:
        return Check(
            "store",
            FAIL,
            {**detail, "error": "not a SQLite database file"},
            hint="point --db at a store, or move the file out of the way and run `fm db init`",
        )
    try:
        connection = schema.connect_ro(path)
    except sqlite3.Error as exc:
        return Check(
            "store", FAIL, {**detail, "error": _short(exc)}, hint=f"inspect it with: sqlite3 {path} '.schema'"
        )
    try:
        try:
            version = schema.read_version(connection)
        except sqlite3.Error as exc:
            return Check(
                "store", FAIL, {**detail, "error": _short(exc)}, hint="not a fitbit-monitor store; point --db at one"
            )
        detail["schema_version"] = version
        detail["schema_known"] = schema.SCHEMA_VERSION
        detail["tables"] = len(schema.tables(connection))
        if version is None:
            status, hint = WARN, "no fm schema in that file: run `fm db migrate`"
        elif version > schema.SCHEMA_VERSION:
            return Check(
                "store",
                FAIL,
                {**detail, "up_to_date": False},
                hint="this store came from a newer fm; upgrade before reading it",
            )
        else:
            pending = [migration.label for migration in schema.pending(connection)]
            detail["pending"] = pending
            detail["up_to_date"] = version >= schema.SCHEMA_VERSION
            status = OK if not pending else WARN
            hint = "" if not pending else "run `fm db migrate`"
        if status == OK and not ctx.quick:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            message = str(integrity[0]) if integrity else "no answer"
            detail["integrity"] = message
            if message != "ok":
                return Check("store", FAIL, detail, hint=f"`sqlite3 {path} 'PRAGMA integrity_check'`")
        elif status == OK:
            detail["integrity"] = "skipped"
        return Check("store", status, detail, hint=hint)
    finally:
        connection.close()


def check_plugins(ctx: Context) -> Check:
    """Where plugins come from, how many loaded, and which ones did not."""
    dirs = list(ctx.config.plugin_dirs)
    missing = [str(directory) for directory in dirs if not directory.is_dir()]
    registry = ctx.session.registry()
    broken = [record.name for record in registry.errors()]
    detail: dict[str, Any] = {
        "dirs": [str(directory) for directory in dirs] or ["-"],
        "dirs_missing": missing,
        "count": len(registry),
        "usable": len(registry.usable()),
        "entry_points": sum(1 for record in registry if record.kind == "entry_point"),
        "builtin": sum(1 for record in registry if record.kind == "builtin"),
    }
    if broken:
        detail["broken"] = broken
        return Check(
            "plugins",
            WARN,
            detail,
            hint="`fm plugins list` prints why each one failed to load",
        )
    if missing:
        return Check("plugins", WARN, detail, hint="a [plugins] dirs entry does not exist (a typo?)")
    if not len(registry):
        return Check("plugins", WARN, detail, hint="nothing discovered: `fm plugin new NAME --dir DIR` to start one")
    return Check("plugins", OK, detail)


def _latest_run(connection: sqlite3.Connection) -> dict[str, Any] | None:
    """The most recent import run, as one row of facts (or nothing)."""
    row = connection.execute(
        "SELECT run_id, MAX(started_at) AS started, COUNT(*) AS datasets,"
        " SUM(CASE WHEN status NOT IN ('ok','skipped','no_data') THEN 1 ELSE 0 END) AS problems,"
        " SUM(rows_inserted) AS rows_inserted, SUM(quarantined) AS quarantined"
        " FROM dataset_run GROUP BY run_id ORDER BY started DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "started": row["started"],
        "datasets": row["datasets"],
        "problems": int(row["problems"] or 0),
        "rows_inserted": int(row["rows_inserted"] or 0),
        "quarantined": int(row["quarantined"] or 0),
    }


def check_imports(ctx: Context) -> Check:
    """The last import: what it was, and whether any dataset in it failed."""
    path = ctx.session.store_path
    if not path.is_file() or path.stat().st_size == 0:
        return Check("imports", SKIPPED, {"store": path}, hint="no store yet, so no import has run")
    try:
        connection = schema.connect_ro(path)
    except sqlite3.Error as exc:  # the store check names this; do not repeat the failure
        return Check("imports", SKIPPED, {"error": _short(exc)})
    try:
        if "dataset_run" not in schema.tables(connection):
            return Check("imports", SKIPPED, {"reason": "no dataset_run table"}, hint="run `fm db migrate`")
        latest = _latest_run(connection)
        quarantined = int(connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0])
        if latest is None:
            return Check(
                "imports",
                OK,
                {"runs": 0, "quarantined": quarantined},
                hint="nothing imported yet: `fm import --dry-run` costs nothing to try",
            )
        detail: dict[str, Any] = {**latest, "quarantined": quarantined}
        if latest["problems"]:
            return Check(
                "imports",
                WARN,
                detail,
                hint="`fm import history` and `fm import report` name the failures",
            )
        if quarantined:
            return Check("imports", WARN, detail, hint="rows were quarantined: `fm store verify` lists them")
        return Check("imports", OK, detail)
    except sqlite3.Error as exc:
        return Check("imports", SKIPPED, {"error": _short(exc)})
    finally:
        connection.close()


def check_clock(ctx: Context) -> Check:
    """Clock sanity, from the machine and from what the store already holds.

    No network time: fm is local-first and this runs offline. What is checked is the
    two things that silently corrupt a store - a system clock that is not in the
    plausible range at all, and timestamps in the future, which mean the clock was
    wrong (or the timezone claim is) when they were written.
    """
    now = ctx.now
    detail: dict[str, Any] = {
        "utc": now,
        "local_offset": _offset_text(datetime.now().astimezone().utcoffset()),
        "tz": "-".join(name for name in time.tzname if name) or "-",
        "uptime_s": round(time.monotonic(), 1),
    }
    if not _CLOCK_MIN_YEAR <= now.year <= _CLOCK_MAX_YEAR:
        return Check(
            "clock",
            FAIL,
            detail,
            hint=f"the system clock says {now.year}; fm stores UTC timestamps and cannot reason about that",
        )
    if not ctx.quick:
        ahead = _future_rows(ctx, now)
        detail["future_rows"] = "skipped" if ahead is None else ahead
        if ahead:
            return Check(
                "clock",
                WARN,
                detail,
                hint=f"{ahead} stored timestamp(s) are ahead of now: the clock was wrong when they were written",
            )
    else:
        detail["future_rows"] = "skipped"
    return Check("clock", OK, detail)


def _future_rows(ctx: Context, now: datetime) -> int | None:
    """How many stored series rows claim to be in the future (None: no store to ask)."""
    path = ctx.session.store_path
    if not path.is_file():
        return None
    cutoff = datetime.fromtimestamp(now.timestamp() - _FUTURE_TOLERANCE_SECONDS, UTC)
    try:
        connection = schema.connect_ro(path)
    except sqlite3.Error:
        return None
    try:
        if "series" not in schema.tables(connection):
            return None
        row = connection.execute(
            "SELECT COUNT(*) FROM series WHERE ts_utc > ?", (cutoff.isoformat(),)
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return None
    finally:
        connection.close()


#: The report, in the order it is printed. Adding a check here is adding a line to
#: ``fm doctor`` and to ``fm doctor --json`` at once.
CHECKS: tuple[Callable[[Context], Check], ...] = (
    check_version,
    check_config,
    check_data_dir,
    check_token,
    check_store,
    check_plugins,
    check_clock,
    check_imports,
)


def run_checks(session: Session, *, quick: bool = False, now: datetime | None = None) -> list[Check]:
    """Every check, in order. A check that raises becomes a ``fail`` naming itself."""
    ctx = Context(session=session, quick=quick, now=now or datetime.now(UTC))
    checks: list[Check] = []
    for check in CHECKS:
        try:
            checks.append(check(ctx))
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not traceback
            checks.append(
                Check(check.__name__.removeprefix("check_"), FAIL, {"error": _short(exc)}, hint="this is a bug in fm")
            )
    return checks


def summary(checks: Sequence[Check]) -> dict[str, int]:
    """Counts per verdict, plus the total - the numbers the summary line prints."""
    tally = {"checks": len(checks), OK: 0, WARN: 0, FAIL: 0, SKIPPED: 0}
    for check in checks:
        tally[check.status] = tally.get(check.status, 0) + 1
    return tally


def report_payload(checks: Sequence[Check], *, quick: bool = False, generated: datetime | None = None) -> dict[str, Any]:
    """The whole report as data (``--json``)."""
    tally = summary(checks)
    return {
        "tool": "fm doctor",
        "report_version": 1,
        "generated": (generated or datetime.now(UTC)).isoformat(timespec="seconds"),
        "quick": quick,
        "exit_code": EXIT_ERROR if any(check.failed for check in checks) else EXIT_OK,
        "summary": tally,
        "checks": [check.as_dict() for check in checks],
    }


def _summary_line(checks: Sequence[Check]) -> str:
    tally = summary(checks)
    hard = [check.name for check in checks if check.failed]
    if hard:
        return f"doctor: {len(hard)} failure(s): {', '.join(hard)}"
    note = "no hard failures" if tally[WARN] or tally[SKIPPED] else "all checks passed"
    return (
        f"doctor: {tally[OK]} ok, {tally[WARN]} warn, {tally[FAIL]} fail, "
        f"{tally[SKIPPED]} skipped - {note}"
    )


def register(sub: "argparse._SubParsersAction") -> None:
    doctor = sub.add_parser(
        "doctor",
        help="diagnose the install: config, credentials, store, plugins, clock",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument(
        "--quick",
        action="store_true",
        help="skip the store scans (integrity check, future-timestamp count)",
    )
    doctor.add_argument(
        "--check",
        dest="only",
        action="append",
        default=[],
        metavar="NAME",
        help=f"run only this check (repeatable): {', '.join(_check_names())}",
    )
    doctor.set_defaults(func=cmd_doctor)
    return doctor


def _check_names() -> list[str]:
    return [check.__name__.removeprefix("check_") for check in CHECKS]


def cmd_doctor(args: argparse.Namespace, session: Session) -> int:
    checks = run_checks(session, quick=bool(args.quick), now=datetime.now(UTC))
    wanted = {str(name).strip() for name in getattr(args, "only", []) or []}
    if wanted:
        unknown = sorted(wanted - {name for name in _check_names()})
        if unknown:
            print(f"doctor: no such check: {', '.join(unknown)} (one of: {', '.join(_check_names())})")
            return EXIT_USAGE
        checks = [check for check in checks if check.name in wanted]
    exit_code = EXIT_ERROR if any(check.failed for check in checks) else EXIT_OK
    if args.json:
        payload = report_payload(checks, quick=bool(args.quick))
        payload["filter"] = sorted(wanted) if wanted else None
        print_json(payload)
        return exit_code
    for check in checks:
        print(check.line())
        if check.hint:
            print(f"{'':<{_STATUS_WIDTH + 2}}hint: {check.hint}")
    print(_summary_line(checks))
    return exit_code


__all__ = [
    "CHECKS",
    "FAIL",
    "OK",
    "SKIPPED",
    "WARN",
    "Check",
    "Context",
    "cmd_doctor",
    "register",
    "report_payload",
    "run_checks",
    "summary",
]
