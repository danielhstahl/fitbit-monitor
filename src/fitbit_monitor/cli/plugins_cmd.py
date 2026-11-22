"""``fm plugins`` and ``fm plugin new``: the author-facing command surface.

    fm plugins list                      what is installed, what is broken
    fm plugins show NAME                 one plugin's options and effective config
    fm plugins validate [NAME...]        load + configure without running anything
    fm plugins run [NAME...] --since 30d compute now, deliver alerts
    fm plugin new NAME                   scaffold a plugin that already passes the tests
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..errors import FmError
from ..plugins.contract import PLUGIN_API_VERSION
from ..plugins.registry import PluginRecord
from ..plugins.runner import run_plugins
from ..plugins.scaffold import scaffold_plugin
from ..plugins.sinks import build_dispatcher
from ..timeutil import DateRange
from .common import EXIT_ERROR, EXIT_OK, EXIT_USAGE, Session, print_json, render_table

RUN_HELP_EPILOG = """\
examples:
  fm plugins run --all --since 30d
  fm plugins run resting_trend --since 8w --json
  fm plugins run resting_trend --since 2026-05-01 --until 2026-05-31 --set resting_trend.hrv_field=sdnn

exit codes:
  0  every plugin ran (including "found nothing" and "no data for this range")
  1  at least one plugin errored, misconfigured itself, or timed out
  2  bad arguments or unreadable config
  4  no local store to read

deliberately out of scope: this is not a daemon. There is no --watch or --interval
repeat flag on purpose - run it from cron, systemd.timer or launchd and get the
same dedup and cooldown behaviour, with your own scheduler's logging.
"""


def register(sub: "argparse._SubParsersAction") -> None:
    plugins = sub.add_parser("plugins", help="list, inspect, validate and run plugins")
    plugins.add_argument("--api", action="store_true", help="print the plugin API version and exit")
    psub = plugins.add_subparsers(dest="plugins_command", required=False)

    plist = psub.add_parser("list", help="show discovered plugins, including broken ones")
    plist.add_argument("--dir", action="append", default=[], help="extra plugins directory (repeatable)")
    plist.add_argument("--json", action="store_true", help="machine-readable output")
    plist.set_defaults(func=cmd_plugins_list)

    pshow = psub.add_parser("show", help="describe one plugin: options, datasets, effective config")
    pshow.add_argument("name")
    pshow.add_argument("--dir", action="append", default=[], help="extra plugins directory (repeatable)")
    pshow.add_argument("--json", action="store_true")
    pshow.set_defaults(func=cmd_plugins_show)

    pvalidate = psub.add_parser("validate", help="load and configure plugins without running them")
    pvalidate.add_argument("names", nargs="*", help="default: every discovered plugin")
    pvalidate.add_argument("--dir", action="append", default=[], help="extra plugins directory (repeatable)")
    pvalidate.add_argument("--json", action="store_true")
    pvalidate.set_defaults(func=cmd_plugins_validate)

    prun = psub.add_parser(
        "run",
        help="run plugins over a date range and deliver their alerts",
        epilog=RUN_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    prun.add_argument("names", nargs="*", help="plugin names (default: all enabled plugins)")
    prun.add_argument("--name", action="append", default=[], help="plugin to run (repeatable); same as a positional")
    prun.add_argument("--all", action="store_true", help="run every enabled plugin")
    prun.add_argument("--since", default="30d", help="start of the range: date, ISO datetime, 7d/12h/3w, today (default 30d)")
    prun.add_argument("--until", default="now", help="end of the range, exclusive (default now)")
    prun.add_argument("--set", dest="overrides", action="append", default=[], metavar="NAME.KEY=VALUE",
                      help="override one config option for this run (repeatable)")
    prun.add_argument("--dir", action="append", default=[], help="extra plugins directory (repeatable)")
    prun.add_argument("--timeout", type=float, default=None, help="wall-clock budget per plugin, seconds")
    prun.add_argument("--json", action="store_true", help="machine-readable report instead of the text summary")
    prun.add_argument("--no-deliver", action="store_true", help="compute and print, but deliver nothing to the sinks")
    prun.add_argument("--no-alerts-file", action="store_true", help="skip the JSONL alert history for this run")
    prun.add_argument("--allow-insecure-webhook", action="store_true",
                      help="permit a plaintext http:// webhook URL (loopback testing only)")
    prun.set_defaults(func=cmd_plugins_run)

    new = sub.add_parser("plugin", help="author a plugin")
    nsub = new.add_subparsers(dest="plugin_command", required=True)
    pnew = nsub.add_parser("new", help="scaffold a plugin that passes the contract tests")
    pnew.add_argument("name", help="lowercase identifier, e.g. spo2_low")
    pnew.add_argument("--dir", default=".", help="where to create the plugin directory (default: cwd)")
    pnew.add_argument("--dataset", default="daily_summary", help="dataset the scaffold reads (default daily_summary)")
    pnew.add_argument("--field", default="resting_hr", help="field the scaffold averages (default resting_hr)")
    pnew.add_argument("--force", action="store_true", help="write into a non-empty directory")
    pnew.add_argument("--json", action="store_true")
    pnew.set_defaults(func=cmd_plugin_new)


# --- commands ---------------------------------------------------------------
def cmd_plugins_list(args: argparse.Namespace, session: Session) -> int:
    registry = session.registry(args.dir)
    if getattr(args, "json", False):
        print_json(
            {
                "plugin_api_version": PLUGIN_API_VERSION,
                "plugin_dirs": [str(p) for p in session.config.plugin_dirs],
                "plugins": registry.as_dicts(),
                "collisions": [c.as_dict() for c in registry.collisions],
            }
        )
        return EXIT_OK
    rows = [record.row() for record in registry]
    for record in registry.errors():
        if record.error is not None:
            rows.append(("", "", "", f"error({record.error.stage}): {record.error}"))
    if not registry:
        dirs = ", ".join(str(p) for p in session.config.plugin_dirs) or "(none configured)"
        print("no plugins found")
        print(f"  searched the fitbit_monitor.plugins entry-point group and these plugins dirs: {dirs}")
        print("  scaffold one with `fm plugin new my_plugin --dir <dir>`")
        return EXIT_OK
    print(render_table(("NAME", "VERSION", "SOURCE", "DATASETS"), rows))
    for collision in registry.collisions:
        print(f"note: {collision.name} declared twice; using {collision.winner} "
              f"(shadowed {collision.loser}: {collision.reason})")
    broken = len(registry.errors())
    if broken:
        print(f"\n{broken} plugin(s) failed to load - run `fm plugins validate` for detail")
    return EXIT_OK


def cmd_plugins_show(args: argparse.Namespace, session: Session) -> int:
    registry = session.registry(args.dir)
    record = registry.get(args.name)
    if record is None:
        print(f"no plugin named {args.name!r}; see `fm plugins list`", file=sys.stderr)
        return EXIT_USAGE
    info = record.describe()
    if record.ok and record.plugin_class is not None:
        section = session.config.plugin_config(args.name, record.plugin_class.declared_defaults())
        info["enabled"] = section.enabled
        info["config"] = section.as_dict()
        unknown = section.unknown_keys
        if unknown:
            info["unknown_config_keys"] = list(unknown)
    if args.json:
        print_json(info)
        return EXIT_OK if record.ok else EXIT_ERROR
    print(f"{info.get('name', args.name)} {info.get('version', '')}".rstrip())
    if info.get("summary"):
        print(f"  {info['summary']}")
    print(f"  source:     {record.kind} ({record.location})")
    print(f"  datasets:   {', '.join(info.get('datasets') or []) or '(none declared)'}")
    print(f"  plugin api: {info.get('requires_api')}")
    if "enabled" in info:
        print(f"  enabled:    {'yes' if info['enabled'] else 'no'}")
    options = info.get("options") or []
    if options:
        print("  options:")
        for option in options:
            default = "" if option.get("default") is None else f" [default: {option['default']}]"
            print(f"    {option['name']:<20} {option['type']:<8} {option.get('help', '')}{default}")
    else:
        print("  options:    (none)")
    if record.error is not None:
        print(f"  ERROR ({record.error.stage}): {record.error}")
        return EXIT_ERROR
    return EXIT_OK


def cmd_plugins_validate(args: argparse.Namespace, session: Session) -> int:
    registry = session.registry(args.dir)
    names = args.names or registry.names()
    results: list[dict[str, Any]] = []
    failures = 0
    for name in names:
        results.append(_validate_one(registry.get(name), name, session))
        failures += 0 if results[-1]["ok"] else 1
    if args.json:
        print_json({"plugin_api_version": PLUGIN_API_VERSION, "results": results})
        return EXIT_ERROR if failures else EXIT_OK
    for result in results:
        status = "ok" if result["ok"] else "FAILED"
        print(f"{result['name']:<24} {status}  {result.get('detail', '')}".rstrip())
    if failures:
        print(f"\n{failures} plugin(s) failed validation")
        return EXIT_ERROR
    print(f"{len(results)} plugin(s) load and configure cleanly")
    return EXIT_OK


def _validate_one(record: PluginRecord | None, name: str, session: Session) -> dict[str, Any]:
    if record is None:
        return {"name": name, "ok": False, "detail": "no such plugin"}
    if not record.ok:
        return {
            "name": name,
            "ok": False,
            "stage": record.error.stage if record.error else "unknown",
            "detail": str(record.error),
        }
    try:
        plugin = record.create()
        config = session.config.plugin_config(name, record.plugin_class.declared_defaults())  # type: ignore[union-attr]
        plugin.configure(config)
    except (FmError, ValueError) as exc:
        return {"name": name, "ok": False, "stage": "configure", "detail": str(exc)}
    except BaseException as exc:  # noqa: BLE001 - validation reports, never propagates
        return {"name": name, "ok": False, "stage": "configure", "detail": f"{type(exc).__name__}: {exc}"}
    detail = f"{record.kind}, {len(record.plugin_class.datasets)} dataset(s), enabled={config.enabled}"  # type: ignore[union-attr]
    unknown = config.unknown_keys
    if unknown:
        detail += f"; unused config keys: {', '.join(unknown)}"
    return {"name": name, "ok": True, "detail": detail, "source": record.kind}


def cmd_plugins_run(args: argparse.Namespace, session: Session) -> int:
    session = session.with_overrides(args.overrides)
    registry = session.registry(args.dir)
    names = list(args.names) + list(args.name)
    if args.all:
        names = []
    unknown = [name for name in names if registry.get(name) is None]
    if unknown and not registry:
        print("no plugins found; see `fm plugins list`", file=sys.stderr)
        return EXIT_USAGE
    timeout = args.timeout if args.timeout is not None else session.config.plugin_timeout
    window: DateRange = session.window(args)
    store = session.read_store()  # before the sinks: a missing store must not be created
    dispatcher = None
    if not args.no_deliver:
        webhook = session.config.webhook
        if args.allow_insecure_webhook:
            from dataclasses import replace as _replace

            webhook = _replace(webhook, allow_insecure=True, enabled=True)
        dispatcher = build_dispatcher(
            session.config,
            store_path=session.store_path,
            data_dir=session.paths.data_dir,
            jsonl=False if args.no_alerts_file else None,
            webhook=webhook,
            printer=lambda text: print(text),
        )
    try:
        report = run_plugins(
            registry,
            store,
            window,
            config=session.config,
            names=names or None,
            timeout=timeout,
            dispatcher=dispatcher,
        )
    finally:
        store.close()
        if dispatcher is not None and dispatcher.history is not None:
            dispatcher.history.close()
    if args.json:
        payload = report.as_dict()
        payload["requested_but_missing"] = unknown
        print_json(payload)
    else:
        print(report.render_text())
        for name in unknown:
            print(f"note: {name} is not an installed plugin")
    return report.exit_code


def cmd_plugin_new(args: argparse.Namespace, session: Session) -> int:
    result = scaffold_plugin(
        args.name,
        args.dir,
        force=args.force,
        dataset=args.dataset,
        field=args.field,
    )
    if args.json:
        print_json(result.as_dict())
        return EXIT_OK
    print(f"created {result.directory}")
    for name in result.files:
        print(f"  {name}")
    print("\nnext:")
    print(f"  fm plugins list --dir {result.directory.parent}")
    print(f"  fm plugins run {result.name} --since 30d --dir {result.directory.parent}")
    print(f"  cd {result.directory} && python -m pytest -q")
    return EXIT_OK


def plugin_api_version() -> int:
    return PLUGIN_API_VERSION


__all__ = ["register"]
