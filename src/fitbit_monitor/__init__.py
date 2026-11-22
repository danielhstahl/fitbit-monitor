"""fitbit-monitor: your own health data, stored locally, watched by plugins.

The package in three layers, each usable on its own:

``fitbit_monitor.store``
    SQLite storage and the read API (range iterators, coverage, downsampling).
``fitbit_monitor.plugins``
    The plugin contract, discovery, the runner, and the alert sinks - the public
    extension surface, documented in ``docs/plugins.md``.
``fitbit_monitor.cli``
    The ``fm`` command line, a thin shell over the two layers above.

Nothing here talks to the network except :class:`~fitbit_monitor.plugins.sinks.WebhookSink`,
and that only if you configure it. See ``docs/data-policy.md``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
