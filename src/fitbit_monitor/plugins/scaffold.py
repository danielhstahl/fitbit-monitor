"""``fm plugin new`` - scaffold a plugin that already passes the contract tests.

The point of a scaffold is not typing less; it is starting from something that
loads, is configured, and is tested on the first try. What this writes:

* ``<dir>/<name>/plugin.py`` - a working ``MonitorPlugin`` with declared options;
* ``<dir>/<name>/test_<name>.py`` - the shared conformance suite from
  :mod:`fitbit_monitor.plugins.testing`, so the new plugin is held to exactly what
  the built-ins are;
* ``<dir>/<name>/pyproject.toml`` - the entry-point declaration that turns the
  directory into an installable distribution;
* ``<dir>/<name>/README.md`` - how to run and ship it.

Drop-in users can skip the packaging entirely and copy ``plugin.py`` into a
configured plugins directory instead - both paths are documented in the README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import UsageError

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


@dataclass(frozen=True)
class Scaffold:
    """The files a scaffold would write (also what ``--dry-run`` prints)."""

    name: str
    directory: Path
    files: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "directory": str(self.directory), "files": list(self.files)}


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
) -> Scaffold:
    """Write the plugin package for ``name`` into ``directory``. Refuses to overwrite."""
    if not _NAME_RE.match(name):
        raise UsageError(
            f"invalid plugin name {name!r}",
            hint="use a lowercase identifier like resting_trend or spo2_low",
        )
    target = Path(directory).expanduser() / name
    if target.exists() and any(target.iterdir()) and not force:
        raise UsageError(
            f"{target} already exists and is not empty",
            hint="pass --force to write into it anyway",
        )
    cls = _class_name(name)
    summary = summary or f"weekly mean of {field} from {dataset}"
    target.mkdir(parents=True, exist_ok=True)
    files = {
        "plugin.py": _PLUGIN_PY.format(cls=cls, name=name, dataset=dataset, field=field, summary=summary),
        f"test_{name}.py": _TEST_PY.format(cls=cls, name=name),
        "pyproject.toml": _PYPROJECT.format(name=name, cls=cls),
        "README.md": _README.format(cls=cls, name=name, dataset=dataset, field=field),
    }
    for filename, content in files.items():
        (target / filename).write_text(content, encoding="utf-8")
    return Scaffold(name=name, directory=target, files=tuple(sorted(files)))


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

_TEST_PY = '''"""Contract tests for `{name}` - the same suite the built-in plugins pass.

Run with: python -m pytest test_{name}.py  (or: python -m unittest -v test_{name})
"""

from __future__ import annotations

from fitbit_monitor.plugins.testing import PluginContractTestCase

from plugin import {cls}


class Test{cls}Contract(PluginContractTestCase):
    plugin_cls = {cls}

    def test_produces_a_metric_on_seeded_data(self) -> None:
        outcome = self.run_fixture_plugin()
        self.assertEqual(outcome.status, "ok", outcome.error)
        self.assertTrue(outcome.metrics, "expected at least one metric from seeded data")
'''

_PYPROJECT = '''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}-fm-plugin"
version = "0.1.0"
description = "A fitbit-monitor plugin"
requires-python = ">=3.11"
dependencies = ["fitbit-monitor"]

# This is the whole installation contract: publish your MonitorPlugin subclass in
# the `fitbit_monitor.plugins` entry-point group and `fm plugins list` finds it.
[project.entry-points."fitbit_monitor.plugins"]
{name} = "{name}.plugin:{cls}"

[tool.setuptools]
packages = ["{name}"]
'''

_README = '''# {name} - fitbit-monitor plugin

Written with `fm plugin new {name}`.

## Try it now, no packaging

Point a plugins directory at the *parent* of this directory:

```sh
fm plugins list --dir ..        # {name} should appear, source=directory
fm plugins run {name} --since 30d
```

`plugin.py` is standalone: you can also copy it into any configured plugins
directory and it will be picked up on the next command.

## Ship it as a package

`pyproject.toml` declares the `fitbit_monitor.plugins` entry point:

```sh
pip install -e .        # or build a wheel and publish it
fm plugins list         # now source=entry_point
```

## Configure it

```toml
# ~/.config/fitbit-monitor/config.toml
[plugins.{name}]
enabled = true
field = "{field}"
dataset = "{dataset}"
```

## Test it

`test_{name}.py` inherits `fitbit_monitor.plugins.testing.PluginContractTestCase`,
the same conformance suite CI runs against the built-in plugins:

```sh
python -m pytest test_{name}.py
```

## Reference

`docs/plugins.md` in the fitbit-monitor repository, and
`fm plugins show {name}` for the option list.
'''

__all__ = ["Scaffold", "scaffold_plugin"]
