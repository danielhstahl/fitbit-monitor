"""``fm plugin new`` - scaffold a plugin that already passes the contract tests.

The point of a scaffold is not typing less; it is starting from something that
loads, is configured, and is tested on the first try - through *both* supported
distribution paths. What this writes (``--dry-run`` prints the same list without
touching the disk):

``<dir>/<name>/<name>/plugin.py``
    a working ``MonitorPlugin`` with declared options, self-contained so the file
    also works when dropped into a plugins directory on its own;
``<dir>/<name>/<name>/__init__.py``
    the package re-export the entry point resolves against;
``<dir>/<name>/tests/test_<name>.py``
    the shared conformance suite from :mod:`fitbit_monitor.plugins.testing`, so the
    new plugin is held to exactly what the built-ins are;
``<dir>/<name>/pyproject.toml``
    the ``fitbit_monitor.plugins`` entry-point declaration that turns the project
    into an installable distribution - with a package layout setuptools accepts;
``<dir>/<name>/README.md``
    how to run, test and ship it.

Two ways to use what it writes, both documented in the generated README:

*drop-in* - point a plugins directory at the project root, because discovery picks
up ``<pkg>/plugin.py`` in every configured directory::

    cd <dir>/<name> && fm plugins list --dir .

*installed* - ``pip install <dir>/<name>``, which publishes the entry point so no
``--dir`` is needed at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import UsageError

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


@dataclass(frozen=True)
class Scaffold:
    """What a scaffold wrote - or, with ``dry_run``, what it would have written."""

    name: str
    directory: Path
    files: tuple[str, ...]
    package: str = ""
    dry_run: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "package": self.package or self.name,
            "directory": str(self.directory),
            "files": list(self.files),
            "dry_run": self.dry_run,
        }


def _class_name(name: str) -> str:
    return "".join(part.title() for part in name.split("_")) + "Plugin"


def scaffold_plugin(
    name: str,
    directory: Path | str,
    *,
    force: bool = False,
    dataset: str = "daily_summary",
    field: str = "resting_hr",
    summary: str | None = None,
    dry_run: bool = False,
) -> Scaffold:
    """Write the plugin project for ``name`` into ``directory``. Refuses to overwrite.

    ``dry_run`` builds the same file list and reports it without creating anything.
    """
    if not _NAME_RE.match(name):
        raise UsageError(
            f"invalid plugin name {name!r}",
            hint="use a lowercase identifier like resting_trend or spo2_low",
        )
    target = Path(directory).expanduser() / name
    if not dry_run and target.exists() and any(target.iterdir()) and not force:
        raise UsageError(
            f"{target} already exists and is not empty",
            hint="pass --force to write into it anyway",
        )
    cls = _class_name(name)
    summary = summary or f"weekly mean of {field} from {dataset}"
    fields = {
        "cls": cls,
        "name": name,
        "dataset": dataset,
        "field": field,
        "summary": summary,
    }
    files: dict[str, str] = {
        "pyproject.toml": _PYPROJECT.format(**fields),
        "README.md": _README.format(**fields),
        f"{name}/__init__.py": _INIT_PY.format(**fields),
        f"{name}/plugin.py": _PLUGIN_PY.format(**fields),
        "tests/__init__.py": _TESTS_INIT.format(**fields),
        f"tests/test_{name}.py": _TEST_PY.format(**fields),
    }
    if not dry_run:
        for relative, content in files.items():
            path = target / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return Scaffold(
        name=name,
        directory=target,
        files=tuple(files),
        package=name,
        dry_run=dry_run,
    )


_PLUGIN_PY = '''"""``{name}`` - a fitbit-monitor plugin (scaffolded by `fm plugin new`)."""

from __future__ import annotations

from typing import Any, Iterable

from fitbit_monitor.plugins import MonitorPlugin, PluginContext, PluginOption, daily_values


class {cls}(MonitorPlugin):
    """{summary}

    Edit `run()` to compute something real. Whatever you compute, report it with
    `ctx.metric(...)` for a value or `ctx.alert(...)` for something the user should
    hear about, and let the host deliver it.
    """

    name = "{name}"
    summary = "{summary}"
    version = "0.1.0"
    datasets = ("{dataset}",)
    options = (
        PluginOption("dataset", "str", "{dataset}", "which stored dataset to read"),
        PluginOption("field", "str", "{field}", "which numeric field of that dataset to average"),
        PluginOption("min_days", "int", 4, "skip weeks with fewer than this many days"),
    )

    def configure(self, config) -> None:
        super().configure(config)
        self.dataset = config.get_str("dataset", "{dataset}")
        self.field = config.get_str("field", "{field}")
        self.min_days = max(1, config.get_int("min_days", 4))
        # Raise PluginConfigError here for anything unusable; the run reports it
        # and every other plugin still runs.

    def run(self, ctx: PluginContext) -> Iterable[Any] | None:
        by_day = daily_values(
            ctx.store.query(self.dataset, ctx.window.since, ctx.window.until, fields=[self.field]),
            self.field,
        )
        if not by_day:
            ctx.log.info("no %s values for %s in %s", self.field, self.dataset, ctx.window)
            return None
        days_expected = max(1, int(ctx.window.days))
        completeness = min(1.0, len(by_day) / days_expected)
        value = sum(by_day.values()) / len(by_day)
        ctx.metric(
            "{name}",
            round(value, 2),
            confidence=completeness,
            details={{
                "dataset": self.dataset,
                "field": self.field,
                "days_present": len(by_day),
                "days_expected": days_expected,
                "complete": len(by_day) >= self.min_days,
            }},
            summary=f"{{value:.1f}} over {{len(by_day)}} days",
        )
        return None
'''

_INIT_PY = '''"""``{name}`` - a fitbit-monitor plugin package (scaffolded by `fm plugin new`).

The class below is what the `fitbit_monitor.plugins` entry point in
`pyproject.toml` resolves to.
"""

from __future__ import annotations

from .plugin import {cls}

__all__ = ["{cls}"]
'''

_TEST_PY = '''"""Contract tests for `{name}` - the same suite the built-in plugins pass.

Run them from the project root, either way:

    python -m pytest -q
    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

from fitbit_monitor.plugins.testing import PluginContractTestCase

from {name}.plugin import {cls}


class Test{cls}Contract(PluginContractTestCase):
    plugin_cls = {cls}

    #: Options to configure the plugin with in every suite run.
    # config_values = {{"min_days": 4}}

    def test_produces_a_metric_on_seeded_data(self) -> None:
        """The suite proves the contract; this proves the metric you came for."""
        outcome = self.run_fixture_plugin()
        self.assertEqual(outcome.status, "ok", outcome.error)
        self.assertTrue(outcome.metrics, "expected at least one metric from seeded data")
        self.assertEqual(outcome.metrics[0].key, "{name}")
'''

_TESTS_INIT = '''"""Tests for the ``{name}`` plugin."""
'''

_PYPROJECT = '''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}-fm-plugin"
version = "0.1.0"
description = "A fitbit-monitor plugin"
requires-python = ">=3.11"

# fitbit-monitor is not on PyPI yet, so an empty list is what actually resolves
# today: install it from the checkout first (`pip install /path/to/fitbit-monitor`)
# and this project builds anywhere. Once it is published, make that:
#   dependencies = ["fitbit-monitor"]
dependencies = []

# This is the whole installation contract: publish your MonitorPlugin subclass in
# the `fitbit_monitor.plugins` entry-point group and `fm plugins list` finds it
# with no configuration. The value is `<package>.<module>:<Class>`.
[project.entry-points."fitbit_monitor.plugins"]
{name} = "{name}.plugin:{cls}"

# The layout on disk is `<project>/<name>/plugin.py`, so the package directory
# exists relative to this file: setuptools fails the build if it is renamed or moved.
[tool.setuptools]
packages = ["{name}"]

[tool.pytest.ini_options]
testpaths = ["tests"]
'''

_README = '''# {name} - fitbit-monitor plugin

Written with `fm plugin new {name}`. It loads, runs and passes the shared contract
suite before you have written any logic.

```
{name}/
├── pyproject.toml            # packaging + the fitbit_monitor.plugins entry point
├── README.md                 # this file
├── {name}/
│   ├── __init__.py
│   └── plugin.py             # <- your code goes here
└── tests/
    ├── __init__.py
    └── test_{name}.py        # the shared PluginContractTestCase
```

## Try it now, without packaging

From this directory. Discovery picks up `<pkg>/plugin.py` in every plugins
directory, so the project root is a valid drop-in directory:

```sh
fm plugins list --dir .
fm plugins run {name} --since 30d --dir .
```

Or put the project root in your config and drop the `--dir`:

```toml
# ~/.config/fitbit-monitor/config.toml
[plugins]
dirs = ["~/fm-plugins/{name}"]
```

`plugin.py` is deliberately self-contained: you can copy that one file into any
configured plugins directory and it is picked up on the next command. Keep it that
way - drop-in mode imports the file by path, so relative imports inside it fail.

## Ship it as a package

`pyproject.toml` declares the entry point, so an install needs no configuration:

```sh
pip install /path/to/fitbit-monitor     # the host, until it is on PyPI
pip install -e .                        # or build a wheel and publish it
fm plugins list                         # now source=entry_point, no --dir
```

## Configure it

```toml
# ~/.config/fitbit-monitor/config.toml
[plugins.{name}]
enabled = true
field = "{field}"
dataset = "{dataset}"
```

`fm plugins show {name}` lists the declared options and the effective values, and
names any key you spelled wrong.

## Test it

`tests/test_{name}.py` inherits `fitbit_monitor.plugins.testing.PluginContractTestCase`,
the same conformance suite CI runs against the built-in plugins:

```sh
python -m pytest -q
python -m unittest discover -s tests -t .
```

## Reference

`docs/plugins.md` in the fitbit-monitor repository, and
`fm plugins show {name}` for the option list.
'''

__all__ = ["Scaffold", "scaffold_plugin"]
