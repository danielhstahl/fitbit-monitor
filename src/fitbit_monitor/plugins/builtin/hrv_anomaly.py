"""``hrv-anomaly``: alert on nights of HRV that are unusual *for you*.

Why a second anomaly plugin, when ``hr-anomaly`` is already here: HRV is not a smaller
version of heart rate, it is a different shape of data. ``heart_rate`` is ~1 440 samples a
day, so the intraday detector can lean on minutes of evidence, still/rest contexts and
exercise windows. ``hrv`` is **one number per night**. Applying "intraburst sustained
minutes" to it would be a rule with no referent - there are no minutes in a nightly value,
so a minutes floor cannot filter anything and there is no burst to sustain through. That is
not a detail the existing engine can be configured around: it is the wrong shape of model,
which is why the statistics live in :mod:`fitbit_monitor.anomaly.hrv` and this file is the
policy and the prose around them.

What the nightly shape needs instead, in one paragraph: a bar built from your own past
nights - median and MAD of ``log(value)`` so a spread means the same fraction whatever your
absolute HRV is - and three rules: ``hrv_low`` (``min_consecutive_nights`` nights in a row,
or one night past ``deep_dip_z``), ``hrv_high`` (the mirror, **off** by default because
unusually high HRV is not a warning sign and an alert on good news trains you to ignore the
ones that are not), and ``corroboration`` by resting heart rate, which is recorded and never
required. A night that breaks the bar does not join the pool the bar is built from, so a long
dip cannot redefine "low" around itself and then report nothing.

The baseline is unapologetic about being provisional: below ``min_baseline_nights`` the bar
is built anyway, flagged, and capped at ``provisional_confidence_cap`` - "no baseline yet" is
a fact about your history, not evidence that nothing is happening - while below
``provisional_min_nights`` the rules do not run at all and the run says so instead of
returning a quiet zero.

Everything here describes your own pattern and none of it is medical advice: what is unusual
for you is not the same as what is wrong, and a HRV dip has a hundred ordinary explanations
you already know about. Why this is its own detector rather than a mode of the
intraday one is
``docs/adr/0006-nightly-series-detector.md``; what it says on the wire is
``docs/plugins.md``.

Configured under ``[plugins.hrv-anomaly]``::

    [plugins.hrv-anomaly]
    enabled = true
    field = "rmssd"          # or auto | deep_sleep_rmssd | sdnn
    z_low = 3.0              # sigmas below your own median before a night counts
    min_consecutive_nights = 2
    corroborate = true
    min_confidence = 0.3     # the plugin's own gate, above the detector's confidence

Run it with ``fm plugins run --name hrv-anomaly --since 30d``, look at its options with
``fm plugins show hrv-anomaly``, and see what it held back in the ``hrv_anomaly_suppressed``
metric of ``fm plugins run --json``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

from ...anomaly.config import AnomalyConfigError
from ...anomaly.hrv import (
    FINDING_HRV_HIGH,
    FINDING_HRV_LOW,
    HRV_FINDING_TYPES,
    HrvAnomalyConfig,
    HrvDetectionResult,
    HrvFinding,
    detect_hrv,
)
from ...config import PluginConfig
from ...store.reader import Coverage
from ...timeutil import DateRange
from ..contract import MonitorPlugin, PluginContext, PluginOption
from ..errors import PluginConfigError
from ..models import Alert, Severity
from .hr_anomaly import NON_DIAGNOSTIC_CODE, HeldBack, thin_window_reason

# The option-readers and the thin-window sentence below are hr_anomaly's, imported rather
# than duplicated: three built-ins reading ``min_confidence`` - or explaining a sparse window -
# three different ways is the drift a shared helper exists to stop. If a fourth plugin needs
# them they should move to ``plugins/_options.py``.
from .hr_anomaly import _duration, _fraction

#: The engine's knobs, generated from the config dataclass rather than copied - see
#: :meth:`HrvAnomalyConfig.option_specs`.
ENGINE_OPTIONS: tuple[PluginOption, ...] = tuple(
    PluginOption(name, kind, default, help_text)
    for name, kind, default, help_text in HrvAnomalyConfig.option_specs()
)
ENGINE_OPTION_NAMES: frozenset[str] = frozenset(opt.name for opt in ENGINE_OPTIONS)

#: This plugin's own policy, kept apart from the detector's statistics for the same reason
#: ``hr-anomaly`` keeps its policy separate: the detector should be answerable without
#: knowing how loudly anyone intends to shout.
POLICY_OPTIONS: tuple[PluginOption, ...] = (
    PluginOption(
        "min_confidence",
        "float",
        0.3,
        "a finding below this confidence is computed and reported, but does not alert",
    ),
    PluginOption(
        "warning_confidence",
        "float",
        0.45,
        "confidence at which an alert leaves informational and becomes warning",
    ),
    PluginOption(
        "critical_confidence",
        "float",
        0.8,
        "confidence at which an alert becomes critical; never reachable on a provisional baseline",
    ),
    PluginOption(
        "min_window_coverage",
        "float",
        0.5,
        "fraction of nights in the window that must have an HRV value before anything is judged",
    ),
    PluginOption("max_alerts", "int", 5, "most significant alerts this one run may raise"),
    PluginOption(
        "cooldown",
        "duration",
        "2d",
        "two runs of nights this close together are one story, not two alerts",
    ),
)

DEFAULT_HRV_COOLDOWN = timedelta(days=2)

#: How the sinks are told this alert's wording is chosen, not measured. Same code as the
#: intraday plugin, because it is the same promise.
HRV_FRAMING_CODE = NON_DIAGNOSTIC_CODE

#: This plugin's own version of the framing sentence. The code is shared with ``hr-anomaly``
#: but the sentence cannot be: "what your rate did" is not what an HRV alert saw, and a
#: disclaimer that does not describe its own signal is decoration.
NON_DIAGNOSTIC_HRV = (
    "This is a heuristic pattern in your own recorded data, not a diagnosis and not medical "
    "advice: a wrist-worn consumer sensor estimates one HRV number per night, and it cannot "
    "tell you what caused it, what it means, or whether you felt fine the next day."
)


def _sentence(text: str) -> str:
    """End a quoted fragment with a full stop, if it does not already have one."""
    return text if text.endswith((".", "!", "?")) else text + "."


#: Headlines, one per finding type. The number goes in the headline because a notification is
#: read before it is opened, and "HRV anomaly detected" is not a sentence anyone can act on.
TITLES = {
    FINDING_HRV_LOW: "HRV low: {observed:.0f} ms ({span})",
    FINDING_HRV_HIGH: "HRV high: {observed:.0f} ms ({span})",
}

#: Findings are listed in metrics and ``--json`` at this size, because the full detail set
#: belongs in the alert row, not in every metric.
_MAX_LISTED = 20


class HrvAnomalyPlugin(MonitorPlugin):
    """Alert on runs of nights whose HRV is far enough from the wearer's own median to matter."""

    name = "hrv-anomaly"
    summary = (
        "alerts when a run of nights has HRV far from your own nightly median and spread, "
        "with the numbers that made it an alert attached"
    )
    version = "0.1.0"
    #: ``daily_summary`` is only read when ``corroborate`` is on, and its absence is a
    #: reported fact rather than a failure; declared here so ``fm plugins list`` and the
    #: host's "no data for this range" answer match what the plugin actually opens.
    datasets = ("hrv", "daily_summary")
    options = ENGINE_OPTIONS + POLICY_OPTIONS

    # --- configuration ------------------------------------------------------
    def configure(self, config: PluginConfig) -> None:
        """Read every option once. Anything nonsensical is refused here, not at run time."""
        super().configure(config)
        self.min_confidence = _fraction(config, "min_confidence", 0.3)
        self.warning_confidence = _fraction(config, "warning_confidence", 0.45)
        self.critical_confidence = _fraction(config, "critical_confidence", 0.8)
        self.min_window_coverage = _fraction(config, "min_window_coverage", 0.5)
        if self.warning_confidence > self.critical_confidence:
            raise PluginConfigError(
                f"{self.name}: warning_confidence ({self.warning_confidence}) must not be above "
                f"critical_confidence ({self.critical_confidence}): an alert would have to be both "
                "to leave informational level",
                plugin=self.name,
            )
        self.max_alerts = config.get_int("max_alerts", 5)
        if self.max_alerts < 1:
            raise PluginConfigError(
                f"{self.name}: max_alerts must be at least 1, got {self.max_alerts} "
                "(0 means 'never alert', which `enabled = false` says better)",
                plugin=self.name,
            )
        self.cooldown = _duration(config, "cooldown", DEFAULT_HRV_COOLDOWN)
        if self.cooldown < timedelta(0):
            raise PluginConfigError(
                f"{self.name}: cooldown must not be negative, got {self.cooldown}", plugin=self.name
            )
        self.anomaly = self._engine_config(config)

    def _engine_config(self, config: PluginConfig) -> HrvAnomalyConfig:
        """The detector's configuration, built from this plugin's declared options only.

        Keys the plugin does not declare are *not* passed on: a typo in ``config.toml`` is the
        runner's "unused config keys" note, not a failure here - the same bargain
        ``hr-anomaly`` offers, and the reason this filters rather than forwards the mapping.
        """
        raw = {key: value for key, value in config.as_dict().items() if key in ENGINE_OPTION_NAMES}
        try:
            return HrvAnomalyConfig.from_mapping(raw)
        except AnomalyConfigError as exc:
            # The detector's own error, re-labelled for the host and kept whole: its `hint`
            # is the actionable half ("raise baseline_nights to at least 10") and a plugin
            # error that drops it leaves the reader hunting through the option list.
            raise PluginConfigError(f"{self.name}: {exc}", plugin=self.name, hint=exc.hint) from exc

    # --- running ------------------------------------------------------------
    def run(self, ctx: PluginContext) -> Iterable[Any] | None:
        window: DateRange = ctx.window
        coverage = ctx.store.coverage(self.anomaly.dataset, window.since, window.until)
        if coverage.fraction < self.min_window_coverage:
            self._decline(ctx, coverage)
            return None

        ctx.check_timeout()
        result = detect_hrv(ctx.store, window.since, window.until, config=self.anomaly)
        for note in result.notes:
            ctx.log.info("%s: %s", note.rule, note.reason)
        accepted, held = self.plan(result)
        for finding in accepted:
            ctx.check_timeout()
            self.emit_alert(ctx, finding)
        self._emit_baseline(ctx, result)
        self._emit_findings(ctx, result, accepted)
        ctx.metric(
            "hrv_anomaly_alerts",
            len(accepted),
            unit="alerts",
            summary=f"{len(accepted)} of {len(result.findings)} finding(s) became alerts",
            details={"policy": self.policy(), "suppressed": len(held)},
        )
        self._emit_suppressed(ctx, held, result, findings=len(result.findings))
        return None

    def _decline(self, ctx: PluginContext, coverage: Coverage) -> None:
        """Too few nights in the window to mean anything: say so in every channel, alert nothing.

        Three channels on purpose, the same way ``hr-anomaly`` does it: a log line for a cron
        log, a string-valued metric for the plain-text report (which otherwise shows only
        ``key = value``), and the reason in ``--json`` details where a script looks. The
        sentence itself is ``hr_anomaly.thin_window_reason``, shared rather than reworded: two
        built-ins explaining the same shortage in two different voices is the drift a shared
        helper exists to prevent.
        """
        window = ctx.window
        reason = thin_window_reason(coverage, self.min_window_coverage)
        ctx.log.info("%s", reason)
        ctx.metric(
            "hrv_anomaly_declined",
            reason,
            summary="no analysis and no alerts: too few nights with HRV in this window",
            details={**coverage.as_dict(), "min_window_coverage": self.min_window_coverage},
        )
        self._emit_suppressed(
            ctx,
            [
                HeldBack(
                    source=self.name,
                    code="thin_window",
                    finding_type="",
                    context="night",
                    onset=window.since,
                    offset=window.until,
                    reason=reason,
                )
            ],
            None,
        )
        ctx.metric("hrv_anomaly_findings", 0, unit="findings", summary="not analysed: coverage too thin")
        ctx.metric("hrv_anomaly_alerts", 0, unit="alerts", summary="no alerts: coverage too thin")

    # --- the alerting policy -------------------------------------------------
    def plan(self, result: HrvDetectionResult) -> tuple[list[HrvFinding], list[HeldBack]]:
        """Which findings alert, and the reason for every one that does not.

        The detector's own suppressions come along as ``source="hrv-anomaly"`` entries with
        their detector reasons, because the most common question about a nightly detector is
        "was there something you decided not to tell me about" - and the answer is usually yes,
        and it is usually one night.
        """
        held: list[HeldBack] = [
            HeldBack(
                source="hrv-anomaly",
                code="night_gate",
                finding_type=item.type,
                context="night",
                onset=_at_midnight(item.day),
                offset=_at_midnight(item.day) + timedelta(days=item.nights),
                reason=item.reason,
                observed=item.value_ms,
            )
            for item in result.suppressed
        ]
        considered: list[HrvFinding] = []
        for finding in result.findings:
            if finding.confidence < self.min_confidence:
                held.append(
                    HeldBack(
                        source="hrv-anomaly",
                        code="low_confidence",
                        finding_type=finding.type,
                        context="night",
                        onset=finding.onset,
                        offset=finding.offset,
                        reason=(
                            f"confidence {finding.confidence:.2f} is below this plugin's "
                            f"min_confidence of {self.min_confidence:g}"
                        ),
                        observed=finding.observed_ms,
                    )
                )
                continue
            considered.append(finding)

        ranked = sorted(
            considered,
            key=lambda finding: (-self.severity_for(finding).rank, -finding.confidence, finding.onset),
        )
        chosen: list[HrvFinding] = []
        for finding in ranked:
            if len(chosen) >= self.max_alerts:
                held.append(
                    HeldBack(
                        source="hrv-anomaly",
                        code="over_run_cap",
                        finding_type=finding.type,
                        context="night",
                        onset=finding.onset,
                        offset=finding.offset,
                        reason=(
                            f"this run already carries its {self.max_alerts} most significant "
                            "alert(s); run a narrower window or raise max_alerts to see the rest"
                        ),
                        observed=finding.observed_ms,
                    )
                )
                continue
            chosen.append(finding)

        chosen.sort(key=lambda finding: finding.onset)
        accepted: list[HrvFinding] = []
        latest: dict[str, HrvFinding] = {}
        for finding in chosen:
            previous = latest.get(finding.type)
            if previous is not None and finding.onset - previous.onset <= self.cooldown:
                held.append(
                    HeldBack(
                        source="hrv-anomaly",
                        code="cooldown",
                        finding_type=finding.type,
                        context="night",
                        onset=finding.onset,
                        offset=finding.offset,
                        reason=(
                            f"only {(finding.onset - previous.onset).days} day(s) after the "
                            f"`{previous.dedup_key}` alert this run already raised, inside this "
                            f"plugin's cooldown of {self.cooldown}; repeats across runs are the "
                            "sink's `alerts.cooldown` to decide"
                        ),
                        observed=finding.observed_ms,
                    )
                )
                continue
            latest[finding.type] = finding
            accepted.append(finding)

        held.sort(key=lambda item: (item.onset, item.code))
        return accepted, held

    def severity_for(self, finding: HrvFinding) -> Severity:
        """How loudly to knock - never what to call it.

        Derived from the detector's own numbers: a finding whose bar is not yet established is
        informational at most, because "unusual for you" after four nights is a weaker sentence
        than after four weeks, and ``provisional_confidence_cap`` already limits how far the
        confidence can reach.
        """
        if not finding.baseline_established:
            return Severity.INFO
        if finding.confidence >= self.critical_confidence:
            return Severity.CRITICAL
        if finding.confidence >= self.warning_confidence:
            return Severity.WARNING
        return Severity.INFO

    def policy(self) -> dict[str, Any]:
        """The policy in force, as data - printed in ``--json`` and attached to every alert."""
        return {
            "field": self.anomaly.field,
            "min_confidence": self.min_confidence,
            "warning_confidence": self.warning_confidence,
            "critical_confidence": self.critical_confidence,
            "min_window_coverage": self.min_window_coverage,
            "max_alerts": self.max_alerts,
            "cooldown": str(self.cooldown),
            "cooldown_seconds": self.cooldown.total_seconds(),
            "alerted_types": [FINDING_HRV_LOW] + ([FINDING_HRV_HIGH] if self.anomaly.alert_on_high else []),
        }

    # --- the prose -----------------------------------------------------------
    def emit_alert(self, ctx: PluginContext, finding: HrvFinding) -> Alert:
        """One alert: every technical field from the detector, all the prose from here."""
        args = finding.alert_args()
        return ctx.alert(
            args["key"],
            self.title_for(finding),
            self.message_for(finding),
            occurred_at=args["occurred_at"],
            severity=self.severity_for(finding),
            confidence=args["confidence"],
            evidence=args["evidence"],
            window_start=args["window_start"],
            window_end=args["window_end"],
            dedup_key=args["dedup_key"],
            labels={
                **args["labels"],
                "framing": HRV_FRAMING_CODE,
                "device_class": "consumer_wrist_wearable",
                "severity_source": "confidence_and_baseline_maturity",
            },
            details={
                **args["details"],
                "non_diagnostic": True,
                "framing": NON_DIAGNOSTIC_HRV,
                "policy": self.policy(),
                "history_command": f"fm alerts list --plugin {self.name}",
            },
        )

    def title_for(self, finding: HrvFinding) -> str:
        return TITLES[finding.type].format(observed=finding.observed_ms, span=self.span_for(finding))

    def span_for(self, finding: HrvFinding) -> str:
        nights = finding.night_count
        if nights == 1:
            return f"{finding.nights[0]}, one night"
        return f"{finding.nights[0]} to {finding.nights[-1]}, {nights} nights"

    def message_for(self, finding: HrvFinding) -> str:
        return " ".join(
            (
                self.evidence_sentence(finding),
                self.baseline_sentence(finding),
                self.corroboration_sentence(finding),
                NON_DIAGNOSTIC_HRV,
                (
                    "To see every night and what was held back: "
                    f"fm plugins run --name {self.name} --since 30d --json"
                ),
            )
        )

    def evidence_sentence(self, finding: HrvFinding) -> str:
        """The nights, the value and the bar - the part a reader can check."""
        values = ", ".join(f"{day.strftime('%d %b')}: {value:.0f} ms" for day, value in finding.values)
        return (
            f"{finding.field} was {finding.observed_ms:.0f} ms over "
            f"{self.span_for(finding)} ({values}), against a bar of {finding.threshold_ms:.0f} ms. "
        )

    def baseline_sentence(self, finding: HrvFinding) -> str:
        """Where the bar came from, and how far the nights were from it."""
        return (
            f"Your own median over the preceding {finding.baseline_nights} nights was "
            f"{finding.baseline_level_ms:.0f} ms with a spread of {finding.baseline_spread_ms:.0f} ms, so these "
            f"nights sat {abs(finding.z_worst):.1f} of those sigmas "
            f"{'below' if finding.type == FINDING_HRV_LOW else 'above'} it, because "
            f"{finding.because}."
            + ("" if finding.baseline_established else " That bar is provisional: it came from few nights.")
        )

    def corroboration_sentence(self, finding: HrvFinding) -> str:
        note = (finding.corroboration or {}).get("note")
        if not note:
            return ""
        return f"Resting heart rate: {_sentence(str(note))}"

    # --- metrics -------------------------------------------------------------
    def _emit_baseline(self, ctx: PluginContext, result: HrvDetectionResult) -> None:
        """The bar, as a string metric: what "normal" meant on this run.

        A number-valued metric cannot carry "provisional, from 5 nights, on sdnn because rmssd
        was empty", and that sentence is the difference between a quiet run meaning something
        and meaning nothing.
        """
        base = result.baseline
        if base.usable:
            text = (
                f"{result.field}: median {base.level_ms:.1f} ms, spread {base.spread_percent:.1f}% "
                f"({base.spread_ms:.1f} ms) over {base.nights} nights -> low bar "
                f"{base.low_bar_ms:.1f} ms, high bar {base.high_bar_ms:.1f} ms"
                + ("" if base.established else f" [{base.reason or 'provisional'}]")
            )
        else:
            text = f"{result.field}: no baseline yet - {base.reason or 'not enough nights'}"
        ctx.metric(
            "hrv_anomaly_baseline",
            text,
            summary=(
                f"{result.nights_seen} of {result.nights_expected} nights had a {result.field} value "
                f"in {ctx.window}"
            ),
            details=result.baseline.as_dict() | {"field_note": result.field_note},
        )

    def _emit_findings(
        self, ctx: PluginContext, result: HrvDetectionResult, accepted: Iterable[HrvFinding]
    ) -> None:
        alerted = {finding.dedup_key for finding in accepted}
        by_type: dict[str, int] = {}
        for finding in result.findings:
            by_type[finding.type] = by_type.get(finding.type, 0) + 1
        ctx.metric(
            "hrv_anomaly_findings",
            len(result.findings),
            unit="findings",
            summary=(
                "no night in this window was unusual for you"
                if not result.findings
                else f"{len(result.findings)} run(s) of nights crossed a rule: "
                + ", ".join(f"{count} {kind}" for kind, count in sorted(by_type.items()))
            ),
            details={
                "by_type": by_type,
                "findings": [finding.as_dict() for finding in result.findings[:_MAX_LISTED]],
                "listed": min(len(result.findings), _MAX_LISTED),
                "alerted": sorted(alerted),
                "coverage": round(result.coverage, 4),
            },
        )

    def _emit_suppressed(
        self,
        ctx: PluginContext,
        held: Iterable[HeldBack],
        result: HrvDetectionResult | None,
        *,
        findings: int = 0,
    ) -> None:
        items = list(held)
        by_code: dict[str, int] = {}
        for item in items:
            by_code[item.code] = by_code.get(item.code, 0) + 1
        if items:
            summary = f"{len(items)} candidate(s) did not become alerts"
        elif findings:
            summary = f"nothing was held back: all {findings} finding(s) became alerts"
        else:
            summary = "nothing crossed a rule in this window, and nothing was held back"
        ctx.metric(
            "hrv_anomaly_suppressed",
            len(items),
            unit="candidates",
            summary=summary,
            details={
                "by_code": by_code,
                "candidates": [item.as_dict() for item in items[:_MAX_LISTED]],
                "listed": min(len(items), _MAX_LISTED),
                "policy": self.policy(),
                "rules_that_did_not_run": [note.as_dict() for note in (result.notes if result else ())],
            },
        )


def _at_midnight(day: date) -> datetime:
    """UTC midnight on ``day`` - the timestamp a nightly candidate is anchored at."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


__all__ = [
    "DEFAULT_HRV_COOLDOWN",
    "HRV_FRAMING_CODE",
    "NON_DIAGNOSTIC_HRV",
    "ENGINE_OPTIONS",
    "HRV_FINDING_TYPES",
    "HrvAnomalyPlugin",
    "POLICY_OPTIONS",
    "TITLES",
]
