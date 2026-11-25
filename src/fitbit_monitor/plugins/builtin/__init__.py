"""Built-in plugins that ship with fitbit-monitor.

These are reference implementations, not extras: the docs point at
:mod:`resting_trend` as the example to copy for a metric, :mod:`hr_anomaly` as the example
for intraday alerts and :mod:`hrv_anomaly` as the example for a *different shape of series*
(one value per night, its own baseline and its own statistics module), and CI runs the shared
contract suite (:mod:`fitbit_monitor.plugins.testing`) against every class listed here, so
the built-ins cannot drift from what third-party authors are held to.

They are discovered as source ``builtin`` - lowest precedence, so a local drop-in
or an installed distribution may shadow any of them.
"""

from __future__ import annotations

from .hr_anomaly import HrAnomalyPlugin
from .hrv_anomaly import HrvAnomalyPlugin
from .resting_trend import RestingTrendPlugin

BUILTIN_PLUGINS: tuple[type, ...] = (RestingTrendPlugin, HrAnomalyPlugin, HrvAnomalyPlugin)

__all__ = [
    "BUILTIN_PLUGINS",
    "HrAnomalyPlugin",
    "HrvAnomalyPlugin",
    "RestingTrendPlugin",
]
