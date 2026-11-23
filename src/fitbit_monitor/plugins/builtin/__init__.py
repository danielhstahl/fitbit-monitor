"""Built-in plugins that ship with fitbit-monitor.

These are reference implementations, not extras: the docs point at
:mod:`resting_trend` as the example to copy for a metric and
:mod:`hr_anomaly` as the example for alerts, and CI runs the shared contract suite
(:mod:`fitbit_monitor.plugins.testing`) against every class listed here, so the
built-ins cannot drift from what third-party authors are held to.

They are discovered as source ``builtin`` - lowest precedence, so a local drop-in
or an installed distribution may shadow any of them.
"""

from __future__ import annotations

from .hr_anomaly import HrAnomalyPlugin
from .resting_trend import RestingTrendPlugin

BUILTIN_PLUGINS: tuple[type, ...] = (RestingTrendPlugin, HrAnomalyPlugin)

__all__ = [
    "BUILTIN_PLUGINS",
    "HrAnomalyPlugin",
    "RestingTrendPlugin",
]
