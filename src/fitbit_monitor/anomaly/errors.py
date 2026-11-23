"""The two failures this package can raise, both :class:`~fitbit_monitor.errors.FmError`.

``AnomalyConfigError`` is a *configuration* refusal - a threshold set that cannot mean
anything (a z-score of 0.2, a baseline that needs more days than the window contains) and
is the CLI's arguments/config problem, hence ``exit_code = 2`` like every other usage
error. ``AnomalyValueError`` is a *data* refusal: somebody handed the engine a finding with
a naive timestamp, an offset before its onset, a confidence of 1.4 or an unknown type.
Neither is ever raised because a wearer's heart rate was surprising: a surprising heart
rate is a finding, not an error.
"""

from __future__ import annotations

from ..errors import FmError


class AnomalyError(FmError):
    """Base class for every expected failure in the anomaly engine."""


class AnomalyConfigError(AnomalyError):
    """The detection thresholds contradict themselves or were the wrong type."""

    exit_code = 2


class AnomalyValueError(AnomalyError):
    """A finding or an episode was built with a field that cannot be true."""

    exit_code = 1


__all__ = ["AnomalyConfigError", "AnomalyError", "AnomalyValueError"]
