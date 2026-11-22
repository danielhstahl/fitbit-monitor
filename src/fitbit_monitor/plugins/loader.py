"""Discovery: where plugins come from.

Two sources, both required by the contract:

``entry_point``
    Installed distributions publishing the ``fitbit_monitor.plugins`` group. This
    is how a plugin lives in its own repo and is installed with ``pip``.

``directory``
    Every ``*.py`` file (and every ``<pkg>/plugin.py``) in each configured
    directory. This is how a user drops a single file in and sees it appear.

Neither source is trusted: importing a plugin can raise anything, and every
failure here is captured as a :class:`~fitbit_monitor.plugins.errors.PluginError`
for the caller to report rather than propagate.
"""

from __future__ import annotations

import importlib.util
import inspect
import itertools
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .contract import MonitorPlugin, validate_plugin_class
from .errors import PluginImportError, PluginRegistrationError

ENTRY_POINT_GROUP = "fitbit_monitor.plugins"
_MODULE_PREFIX = "fitbit_monitor_plugin__"
_COUNTER = itertools.count()


@dataclass
class Candidate:
    """Something that might be a plugin, before it is loaded."""

    kind: str  # "entry_point" | "directory"
    location: str  # entry point name, or the file it came from
    label: str  # what to call it in messages before the class is known
    load: Callable[[], Any]  # returns a class, or a list of classes
    module_hint: str = ""


def iter_entry_point_candidates(group: str = ENTRY_POINT_GROUP) -> list[Candidate]:
    """Candidates from installed distributions. A broken group entry is not fatal."""
    from importlib.metadata import entry_points

    out: list[Candidate] = []
    try:
        discovered = list(entry_points(group=group))
    except Exception as exc:  # pragma: no cover - defensive: broken metadata in site-packages
        return [
            Candidate(
                kind="entry_point",
                location=group,
                label=group,
                load=_raising(exc),
            )
        ]
    for ep in discovered:
        out.append(_candidate_from_entry_point(ep))
    return out


def iter_builtin_candidates() -> list[Candidate]:
    """The plugins that ship with fitbit-monitor (lowest precedence).

    Imported lazily so ``import fitbit_monitor.plugins`` does not import every
    built-in implementation, and so the entry-point group remains optional: a
    checkout on ``PYTHONPATH`` with no installed metadata still lists its examples.
    """
    from .builtin import BUILTIN_PLUGINS

    return [
        Candidate(
            kind="builtin",
            location=f"fitbit_monitor.plugins.builtin.{cls.__name__}",
            label=getattr(cls, "name", "") or cls.__name__,
            load=_returns(cls),
            module_hint=cls.__module__,
        )
        for cls in BUILTIN_PLUGINS
    ]


def _returns(value: Any) -> Callable[[], Any]:
    def load() -> Any:
        return value

    return load


def _candidate_from_entry_point(ep: Any) -> Candidate:
    def load() -> Any:
        try:
            return ep.load()
        except BaseException as exc:  # noqa: BLE001 - plugin code, anything can raise
            raise PluginImportError(
                f"entry point {ep.name!r} failed to load: {type(exc).__name__}: {exc}",
                plugin=ep.name,
                hint="fix or uninstall the distribution that provides this plugin",
            ) from exc

    value = getattr(ep, "value", "")
    dist = getattr(ep, "dist", None)
    dist_name = getattr(dist, "name", None) or ""
    location = f"{dist_name}:{ep.name}" if dist_name else ep.name
    return Candidate(kind="entry_point", location=location, label=ep.name, load=load, module_hint=str(value))


def _raising(exc: BaseException) -> Callable[[], Any]:
    def load() -> Any:
        raise exc

    return load


def iter_directory_candidates(dirs: Iterable[Path | str]) -> list[Candidate]:
    """Candidates found in each drop-in directory, in the order the dirs are given.

    Missing directories are skipped silently (a default that is absent is not an
    error); an unreadable one is reported as a candidate that fails to load, so it
    shows up in ``fm plugins list`` instead of vanishing.
    """
    out: list[Candidate] = []
    for raw in dirs:
        directory = Path(raw).expanduser()
        if not directory.exists():
            continue
        if not directory.is_dir():
            out.append(
                Candidate(
                    kind="directory",
                    location=str(directory),
                    label=directory.name,
                    load=_raising(
                        PluginImportError(f"plugin path is not a directory: {directory}", plugin=directory.name)
                    ),
                )
            )
            continue
        for path in _module_paths(directory):
            out.append(_candidate_from_path(path))
    return out


def _module_paths(directory: Path) -> list[Path]:
    files = sorted(p for p in directory.glob("*.py") if not p.name.startswith(("_", ".")))
    packages = sorted(p / "plugin.py" for p in directory.iterdir() if p.is_dir() and (p / "plugin.py").exists())
    return files + packages


def _candidate_from_path(path: Path) -> Candidate:
    def load() -> Any:
        module = load_module_from_path(path)
        classes = find_plugin_classes(module)
        if not classes:
            raise PluginRegistrationError(
                f"{path} defines no MonitorPlugin subclass",
                plugin=path.stem,
                hint="add `class MyPlugin(MonitorPlugin)` with a `name` attribute",
            )
        return classes

    return Candidate(kind="directory", location=str(path), label=path.stem, load=load)


def load_module_from_path(path: Path) -> types.ModuleType:
    """Import a standalone ``.py`` as a module. Raises PluginImportError on failure."""
    name = f"{_MODULE_PREFIX}{path.stem}_{next(_COUNTER)}"
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise PluginImportError(f"cannot import {path} as a Python module", plugin=path.stem)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException as exc:  # noqa: BLE001 - the whole point: a bad import is data
        sys.modules.pop(name, None)
        raise PluginImportError(
            f"plugin file {path.name} raised {type(exc).__name__} while importing: {exc}",
            plugin=path.stem,
            hint="run `python -c \"import ...\"` on the file to see the traceback",
        ) from exc
    return module


def find_plugin_classes(module: types.ModuleType) -> list[type[MonitorPlugin]]:
    """Concrete MonitorPlugin subclasses *defined* in ``module``, in source order."""
    found: list[type[MonitorPlugin]] = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if not isinstance(obj, type) or obj.__module__ != module.__name__:
            continue
        if not issubclass(obj, MonitorPlugin) or obj is MonitorPlugin:
            continue
        found.append(obj)
    return _dedupe_by_source_order(found, module)


def _dedupe_by_source_order(classes: Sequence[type], module: types.ModuleType) -> list[type]:
    """``inspect.getmembers`` sorts by name; authors expect declaration order."""
    linenos: list[tuple[int, type]] = []
    for cls in classes:
        try:
            linenos.append((inspect.getsourcelines(cls)[1], cls))
        except (OSError, TypeError):  # pragma: no cover - exotic loaders
            linenos.append((0, cls))
    return [cls for _, cls in sorted(linenos, key=lambda item: item[0])]


def coerce_classes(loaded: Any, *, label: str) -> list[type[MonitorPlugin]]:
    """Normalise a loaded value to plugin classes and validate each one."""
    items = list(loaded) if isinstance(loaded, (list, tuple)) else [loaded]
    out: list[type[MonitorPlugin]] = []
    for item in items:
        out.append(validate_plugin_class(item, source=label))
    return out


__all__ = [
    "ENTRY_POINT_GROUP",
    "Candidate",
    "coerce_classes",
    "find_plugin_classes",
    "iter_builtin_candidates",
    "iter_directory_candidates",
    "iter_entry_point_candidates",
    "load_module_from_path",
]
