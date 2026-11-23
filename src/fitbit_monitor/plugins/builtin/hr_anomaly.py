"""``hr-anomaly`` - the reference ALERT plugin.

``resting_trend`` is the example for metrics; this is the example for alerts. It reads
the heart rate already in your store, hands it to :mod:`fitbit_monitor.anomaly` - which
owns every threshold and every statistic - and turns what came back into alerts that the
host delivers through the standard sinks (history, console, JSONL, optional webhook)
with the dedup and cooldown the sinks already do properly.

It adds exactly three things, and they are the three a plugin owns and a library cannot:

**1. The words.** A :class:`~fitbit_monitor.anomaly.models.Finding` is a measurement with
no claim in it; an alert *is* a claim, so the claim gets made here, carefully. Every
message says in plain language that what it describes is a heuristic pattern in the
wearer's own recording from a wrist-worn consumer device - not a diagnosis, not medical
advice, not a statement about what caused anything. Then it names the evidence: the
window, the readings and how much of the span was actually observed, the rate (or the
scatter index, for the rhythm rule) that crossed the bar, the personal level and
threshold it was compared against, and the confidence with an explicit note about what
that number is *not*. Then it hands over a command that runs, so the raw readings are
one line away. ``fm alerts list --plugin hr-anomaly`` is named in the message too,
because "what else has it said" is the first question an alert deserves.

**2. The alerting policy.** Whether a finding is worth a knock (``min_confidence``), how
loudly (``severity``, from confidence and how mature the personal baseline is), how many
one run may send (``max_alerts``) and how close two episodes of the same kind have to be
to count as one message (``cooldown``). Everything held back is a metric, never a
silence. Note the two cooldowns and who owns which: this plugin's ``cooldown`` collapses
repeats *inside one scan*, and ``alerts.cooldown`` in ``config.toml`` - which the sink
applies, not this plugin - decides repeats *across runs*. The shorter of the two wins in
practice, and neither is a claim that an episode stopped happening.

**3. Honesty about thin data.** Below ``min_window_coverage`` this plugin alerts on
nothing at all and says so, because a watch left on a dresser for five days out of seven
is not a quiet heart. The engine applies its own per-episode gates
(:attr:`~fitbit_monitor.anomaly.config.AnomalyConfig.min_coverage`, minimum samples,
minimum duration, the exercise and nocturnal attributions) and reports what it dropped;
this plugin adds the window-level gate, republishes everything the engine suppressed,
and lists the rules that never ran for lack of a baseline. So ``--json`` can always
answer the question a quiet run actually raises: *why was it quiet*.

Configuration is ``[plugins.hr-anomaly]`` in ``config.toml``. Every threshold in
:class:`~fitbit_monitor.anomaly.config.AnomalyConfig` is a declared option here, taken
from ``AnomalyConfig.option_specs()`` rather than copied, so the file, ``fm plugins
show hr-anomaly`` and the numbers printed on each alert cannot disagree with the numbers
the rules ran on::

    fm plugins run hr-anomaly --since 30d
    fm plugins run hr-anomaly --since 2026-05-01 --until 2026-05-31 --json
    fm plugins run --name hr-anomaly --since 90d --set hr-anomaly.z_high=4.5
    fm plugins run hr-anomaly --since 90d --no-deliver     # print, deliver nothing

One caveat the host cannot check for you: ``datasets`` is a class attribute and the
host's "no data for this range" gate reads it before ``configure()`` runs, so if you
point ``dataset`` at another stored series, list that series yourself in a subclass or
the empty-range answer will still be given about ``heart_rate``.

Nothing here writes, waits on a clock, or sends anything anywhere: the store handle is
read-only, the host injects the clock, and the only outbound path in the program is the
webhook the user configures. See ``docs/plugins.md`` and ``docs/detection.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from ...anomaly import (
    FINDING_IRREGULAR,
    FINDING_SUSTAINED_HIGH,
    FINDING_SUSTAINED_LOW,
    FINDING_TYPES,
    AnomalyConfig,
    AnomalyConfigError,
    DetectionResult,
    Finding,
    SuppressedEpisode,
    detect,
)
from ...config import PluginConfig, parse_duration
from ...store.reader import Coverage
from ...timeutil import DateRange
from ..contract import MonitorPlugin, PluginContext, PluginOption
from ..errors import PluginConfigError
from ..models import Alert, Severity

#: How the host's sinks are told this alert's wording is chosen, not measured.
NON_DIAGNOSTIC_CODE = "non_diagnostic_consumer_wearable_heuristic"

#: The sentence every alert says about itself. Kept as one constant so it is one edit,
#: and so a test can assert the framing is present rather than hope it is.
NON_DIAGNOSTIC = (
    "This is a heuristic pattern in your own recorded data, not a diagnosis and not "
    "medical advice: a wrist-worn consumer sensor can see what your rate did, it cannot "
    "tell you what caused it or what it means."
)

#: How to look at the readings an alert was made from. ``{command}`` is filled in per
#: alert; the second command is fixed and names this plugin's own history.
INSPECT_NOTE = (
    "To check it yourself: {command} prints the readings behind this window, and "
    "`fm alerts list --plugin hr-anomaly` lists every episode already flagged."
)

#: One title per rule. Titles are one line, because every sink prints them inline.
TITLES: Mapping[str, str] = {
    FINDING_SUSTAINED_HIGH: "Heart rate held above your own usual level for {span}",
    FINDING_SUSTAINED_LOW: "Heart rate held below your own usual level for {span}",
    FINDING_IRREGULAR: "Beat-to-beat intervals were unusually uneven for {span}",
}

#: How to say "in this context" to a person instead of to a database.
CONTEXT_PHASE: Mapping[str, str] = {
    "rest": "while you were at rest",
    "sleep": "while you were asleep",
}

#: Default for the ``cooldown`` option. Matches ``alerts.cooldown``, so a user who sets
#: neither gets one answer from both layers.
DEFAULT_COOLDOWN = timedelta(hours=36)

#: Default for ``inspect_padding``: the printed export command reaches a little
#: past the episode, because "what happened just before" is usually the first question.
DEFAULT_INSPECT_PADDING = timedelta(minutes=10)

#: How many episodes one metric's ``details`` lists before it switches to counts. A
#: details dict ends up in a database row and a webhook body, so it is summarised.
_MAX_LISTED = 25

#: ``(name, type, default, help)`` for the options this plugin owns on top of the
#: engine's thresholds: all of them are about *what to say and how often*, never about
#: what counts as unusual - that is the engine's decision and lives in its config.
POLICY_OPTIONS: tuple[PluginOption, ...] = (
    PluginOption(
        "min_confidence",
        "float",
        0.3,
        "findings below this confidence are reported as suppressed instead of alerted on",
    ),
    PluginOption(
        "warning_confidence",
        "float",
        0.45,
        "confidence at or above which an alert is a warning rather than informational",
    ),
    PluginOption(
        "critical_confidence",
        "float",
        0.8,
        "confidence at or above which an alert is critical (a loud knock, not a clinical claim)",
    ),
    PluginOption(
        "cooldown",
        "duration",
        "36h",
        "two episodes of the same rule and context this close together are one alert in one run",
    ),
    PluginOption(
        "max_alerts",
        "int",
        10,
        "most alerts one run emits; the rest are reported as suppressed, least significant first",
    ),
    PluginOption(
        "min_window_coverage",
        "float",
        0.5,
        "fraction of the run's days that must have heart-rate data, below it nothing is alerted on",
    ),
    PluginOption(
        "inspect_padding",
        "duration",
        "10m",
        "how far past an episode the printed inspect command reaches, so you see its edges",
    ),
)

#: The engine's thresholds as declared options, straight from the same dataclass the
#: rules read, so this list and those rules cannot drift apart.
ENGINE_OPTIONS: tuple[PluginOption, ...] = tuple(
    PluginOption(name, kind, default, help_text)
    for name, kind, default, help_text in AnomalyConfig.option_specs()
)

#: Which declared options belong to the engine - everything except this plugin's own
#: policy. Used to build the engine's config, and named in ``--json`` output.
ENGINE_OPTION_NAMES: frozenset[str] = frozenset(option.name for option in ENGINE_OPTIONS)

POLICY_OPTION_NAMES: frozenset[str] = frozenset(option.name for option in POLICY_OPTIONS)


@dataclass(frozen=True)
class HeldBack:
    """One candidate episode that did not become an alert, and who decided that.

    ``source`` separates the two authorities: ``engine`` episodes crossed a rule and were
    held by a statistical gate, ``hr-anomaly`` ones were held by the alerting policy
    above. Both are reported, because "nothing happened" and "something happened and here
    is why it stayed quiet" are different answers.
    """

    source: str
    code: str
    finding_type: str
    context: str
    onset: datetime
    offset: datetime
    reason: str
    observed: float | None = None

    @classmethod
    def from_engine(cls, episode: SuppressedEpisode) -> "HeldBack":
        return cls(
            source="engine",
            code="engine_gate",
            finding_type=episode.type,
            context=episode.context,
            onset=episode.onset,
            offset=episode.offset,
            reason=episode.reason,
            observed=episode.observed,
        )

    @classmethod
    def of(cls, finding: Finding, code: str, reason: str) -> "HeldBack":
        return cls(
            source="hr-anomaly",
            code=code,
            finding_type=finding.type,
            context=finding.context,
            onset=finding.onset,
            offset=finding.offset,
            reason=reason,
            observed=finding.observed,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "decided_by": self.source,
            "code": self.code,
            "type": self.finding_type,
            "context": self.context,
            "onset": self.onset.isoformat(),
            "offset": self.offset.isoformat(),
            "duration_minutes": round((self.offset - self.onset).total_seconds() / 60.0, 1),
            "observed": None if self.observed is None else round(self.observed, 2),
            "reason": self.reason,
        }


class HrAnomalyPlugin(MonitorPlugin):
    """Alert on stretches of your own heart rate that are unusual for you."""

    name = "hr-anomaly"
    summary = "alerts when your heart rate strays from your own usual level, with the readings attached"
    version = "0.1.0"
    #: The only dataset this plugin reads. See the module docstring for the caveat if
    #: you repoint the engine's ``dataset`` option somewhere else.
    datasets = ("heart_rate",)
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
                f"critical_confidence ({self.critical_confidence}): an alert would have to be "
                "both to leave informational level",
                plugin=self.name,
            )
        self.max_alerts = config.get_int("max_alerts", 10)
        if self.max_alerts < 1:
            raise PluginConfigError(
                f"{self.name}: max_alerts must be at least 1, got {self.max_alerts} "
                "(0 means 'never alert', which `enabled = false` says better)",
                plugin=self.name,
            )
        self.cooldown = _duration(config, "cooldown", DEFAULT_COOLDOWN)
        if self.cooldown < timedelta(0):
            raise PluginConfigError(
                f"{self.name}: cooldown must not be negative, got {self.cooldown}", plugin=self.name
            )
        self.inspect_padding = _duration(config, "inspect_padding", DEFAULT_INSPECT_PADDING)
        self.anomaly = self._engine_config(config)

    def _engine_config(self, config: PluginConfig) -> AnomalyConfig:
        """The engine's own configuration, built from this plugin's declared options only.

        Keys the plugin does not declare are *not* passed on: a typo in ``config.toml``
        is the runner's "unused config keys" note, not a failure here, which is what
        every other plugin gets and what this one promises too.
        """
        raw = {key: value for key, value in config.as_dict().items() if key in ENGINE_OPTION_NAMES}
        try:
            return AnomalyConfig.from_mapping(raw)
        except AnomalyConfigError as exc:
            raise PluginConfigError(f"{self.name}: {exc}", plugin=self.name, hint=exc.hint) from exc

    # --- running ------------------------------------------------------------
    def run(self, ctx: PluginContext) -> Iterable[Any] | None:
        window: DateRange = ctx.window
        coverage = ctx.store.coverage(self.anomaly.dataset, window.since, window.until)
        if coverage.fraction < self.min_window_coverage:
            self._decline(ctx, coverage)
            return None

        ctx.check_timeout()
        result = detect(ctx.store, window.since, window.until, config=self.anomaly)
        accepted, held = self.plan(result)
        for finding in accepted:
            ctx.check_timeout()
            self.emit_alert(ctx, finding)
        self._emit_coverage(ctx, coverage, declined=None, result=result)
        self._emit_baselines(ctx, result)
        self._emit_findings(ctx, result, accepted)
        ctx.metric(
            "hr_anomaly_alerts",
            len(accepted),
            unit="alerts",
            summary=f"{len(accepted)} of {len(result.findings)} finding(s) became alerts",
            details={"policy": self.policy(), "suppressed": len(held)},
        )
        self._emit_suppressed(ctx, held, result, findings=len(result.findings))
        return None

    def _decline(self, ctx: PluginContext, coverage: Coverage) -> None:
        """Coverage too thin to mean anything: say so in every channel, alert on nothing.

        The reason is emitted three ways on purpose. As a log line it is free for a cron
        log; as a string-valued metric it is in the plain-text report, which otherwise
        shows only ``key = value``; and in ``--json`` it is in the suppressed metric's
        details, which is where a script looks for the reason a run was quiet.
        """
        window = ctx.window
        reason = thin_window_reason(coverage, self.min_window_coverage)
        ctx.log.info("%s", reason)
        ctx.metric(
            "hr_anomaly_declined",
            reason,
            summary="no analysis and no alerts: too little data in this window",
            details={**coverage.as_dict(), "min_window_coverage": self.min_window_coverage},
        )
        self._emit_coverage(ctx, coverage, declined=reason)
        self._emit_suppressed(
            ctx,
            [
                HeldBack(
                    source="hr-anomaly",
                    code="thin_window",
                    finding_type="",
                    context="",
                    onset=window.since,
                    offset=window.until,
                    reason=reason,
                )
            ],
            None,
        )
        ctx.metric("hr_anomaly_findings", 0, unit="findings", summary="not analysed: coverage too thin")
        ctx.metric("hr_anomaly_alerts", 0, unit="alerts", summary="no alerts: coverage too thin")

    # --- the alerting policy -------------------------------------------------

    def plan(self, result: DetectionResult) -> tuple[list[Finding], list[HeldBack]]:
        """Decide which findings become alerts, and record the reason for every one that does not.

        Three gates, in the order they cost least to explain: confidence first (a weak
        finding should not spend one of the run's alert slots), then the per-run cap -
        which keeps the *most* significant findings rather than the earliest, because a
        scan of a quarter should not go quiet about a strong episode just because it
        found three weaker ones first - then this plugin's cooldown, which collapses two
        episodes of the same rule and context that are close enough to be one story.
        """
        held: list[HeldBack] = [HeldBack.from_engine(episode) for episode in result.suppressed]
        considered: list[Finding] = []
        for finding in result.findings:
            if finding.confidence < self.min_confidence:
                held.append(
                    HeldBack.of(
                        finding,
                        "low_confidence",
                        f"confidence {finding.confidence:.2f} is below this plugin's "
                        f"min_confidence of {self.min_confidence:g}",
                    )
                )
                continue
            considered.append(finding)

        ranked = sorted(
            considered,
            key=lambda finding: (-self.severity_for(finding).rank, -finding.confidence, finding.onset),
        )
        chosen: list[Finding] = []
        for finding in ranked:
            if len(chosen) >= self.max_alerts:
                held.append(
                    HeldBack.of(
                        finding,
                        "over_run_cap",
                        f"this run already carries its {self.max_alerts} most significant "
                        "alert(s); run a narrower window or raise max_alerts to see the rest",
                    )
                )
                continue
            chosen.append(finding)

        chosen.sort(key=lambda finding: finding.onset)
        accepted: list[Finding] = []
        latest: dict[tuple[str, str], Finding] = {}
        for finding in chosen:
            key = (finding.type, finding.context)
            previous = latest.get(key)
            if previous is not None and finding.onset - previous.onset <= self.cooldown:
                held.append(
                    HeldBack.of(
                        finding,
                        "cooldown",
                        f"only {_human(finding.onset - previous.onset)} after the "
                        f"`{previous.dedup_key}` alert this run already raised, inside this "
                        f"plugin's cooldown of {_human(self.cooldown)}; repeats across runs "
                        "are the sink's `alerts.cooldown` to decide",
                    )
                )
                continue
            latest[key] = finding
            accepted.append(finding)

        held.sort(key=lambda item: (item.onset, item.code))
        return accepted, held

    def severity_for(self, finding: Finding) -> Severity:
        """How loudly to knock - never what to call it.

        Derived entirely from the engine's own numbers: a finding from a baseline that is
        not yet established is informational at most, because "unusual for you" after
        three days is a weaker sentence than after three weeks. Above that, the two
        confidence bars say warning or critical.
        """
        if not finding.baseline_established:
            return Severity.INFO
        if finding.confidence >= self.critical_confidence:
            return Severity.CRITICAL
        if finding.confidence >= self.warning_confidence:
            return Severity.WARNING
        return Severity.INFO

    def policy(self) -> dict[str, Any]:
        """The alerting policy in force, as data - it is printed in ``--json`` output."""
        return {
            "min_confidence": self.min_confidence,
            "warning_confidence": self.warning_confidence,
            "critical_confidence": self.critical_confidence,
            "cooldown": _human(self.cooldown),
            "cooldown_seconds": self.cooldown.total_seconds(),
            "max_alerts": self.max_alerts,
            "min_window_coverage": self.min_window_coverage,
            "inspect_padding": _human(self.inspect_padding),
            "inspect_padding_seconds": self.inspect_padding.total_seconds(),
        }

    # --- the prose -----------------------------------------------------------
    def emit_alert(self, ctx: PluginContext, finding: Finding) -> Alert:
        """One alert: every technical field from the engine, all the prose from here."""
        args = finding.alert_args()
        details = dict(args["details"])
        # The engine puts the *breakdown of factors* behind the confidence under
        # ``confidence`` in its details. That breakdown is worth keeping and is not the
        # number - ``Alert.confidence`` is the number - so it is renamed rather than
        # left to surprise a consumer who expected a probability.
        if "confidence" in details:
            details["confidence_factors"] = details.pop("confidence")
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
                "framing": NON_DIAGNOSTIC_CODE,
                "device_class": "consumer_wrist_wearable",
                "severity_source": "confidence_and_baseline_maturity",
            },
            details={
                **details,
                "non_diagnostic": True,
                "framing": NON_DIAGNOSTIC,
                "context_phase": CONTEXT_PHASE.get(finding.context, ""),
                "inspect": self.inspect_command(finding),
                "history_command": f"fm alerts list --plugin {self.name}",
                "policy": self.policy(),
            },
        )

    def title_for(self, finding: Finding) -> str:
        return TITLES[finding.type].format(span=f"{finding.duration_minutes:.0f} min")

    def message_for(self, finding: Finding) -> str:
        return " ".join(
            (
                self.evidence_sentence(finding),
                self.confidence_sentence(finding),
                NON_DIAGNOSTIC,
                INSPECT_NOTE.format(command=self.inspect_command(finding)),
            )
        )

    def evidence_sentence(self, finding: Finding) -> str:
        """The window, the readings and the bar - the part a reader can check."""
        phase = CONTEXT_PHASE.get(finding.context, "")
        span = (
            f"{finding.onset.strftime('%Y-%m-%d %H:%M')} to {finding.offset.strftime('%H:%M')} UTC"
            f"{(' ' + phase) if phase else ''} "
            f"({finding.duration_minutes:.0f} min, {finding.samples} readings, "
            f"{finding.coverage:.0%} of that span actually recorded)"
        )
        if finding.type == FINDING_IRREGULAR:
            return (
                f"Your beat-to-beat interval was far more uneven than your own ordinary "
                f"unevenness for {span}: the scatter index reached {finding.observed:.2f} "
                f"against your usual {finding.baseline_level:.2f}, past a bar of "
                f"{finding.threshold:.2f} ({finding.margin_sigma:.1f} times your usual spread)."
            )
        above = finding.type == FINDING_SUSTAINED_HIGH
        extreme, verb = ("higher", "peaked") if above else ("lower", "dropped to")
        return (
            f"Your heart rate sat {extreme} than your own usual level for {span}: it averaged "
            f"{finding.mean:.1f} bpm and {verb} {finding.observed:.1f} bpm, against a personal "
            f"bar of {finding.threshold:.1f} bpm built from your own usual level of "
            f"{finding.baseline_level:.1f} bpm ({finding.margin:.1f} bpm past it, "
            f"{finding.margin_sigma:.1f} times your own usual spread)."
        )

    def confidence_sentence(self, finding: Finding) -> str:
        """What the number means, and the two things it does not mean."""
        if finding.baseline_established:
            maturity = f"the personal baseline behind it is {finding.baseline_days} days of data"
        else:
            maturity = (
                f"only {finding.baseline_days} usable day(s) so far, so it is capped at "
                f"{self.anomaly.provisional_confidence_cap:g} until more of your own history is stored"
            )
        return (
            f"Confidence {finding.confidence:.2f} says how far those readings sat from your "
            f"own ordinary spread and how complete the recording was ({maturity}) - it is "
            "not a probability that anything is wrong, and it is not comparable between people."
        )

    def inspect_command(self, finding: Finding) -> str:
        """A copy-pasteable command that prints the readings this finding was made from."""
        since = finding.onset - self.inspect_padding
        until = finding.offset + self.inspect_padding
        return (
            f"fm store export --dataset {self.anomaly.dataset} "
            f"--since {since.isoformat()} --until {until.isoformat()} --format csv"
        )

    # --- metrics -------------------------------------------------------------
    def _emit_coverage(
        self,
        ctx: PluginContext,
        coverage: Coverage,
        *,
        declined: str | None = None,
        result: DetectionResult | None = None,
    ) -> None:
        details: dict[str, Any] = {
            **coverage.as_dict(),
            "min_window_coverage": self.min_window_coverage,
            "too_thin_to_alert": declined is not None,
        }
        if declined:
            details["declined"] = declined
        if result is not None:
            details["days_scanned"] = result.days_scanned
            details["samples_scanned"] = result.samples_scanned
        ctx.metric(
            "hr_anomaly_data_coverage",
            round(coverage.fraction, 4),
            unit="fraction of days",
            confidence=round(coverage.fraction, 4),
            summary=(
                f"{coverage.days_present} of {coverage.days_expected} days in the window had "
                f"{coverage.dataset} readings"
            ),
            details=details,
        )

    def _emit_baselines(self, ctx: PluginContext, result: DetectionResult) -> None:
        """What every threshold was built from - including the ones that refused to be built."""
        for name, baseline in sorted(result.baselines.items()):
            state = (
                "established"
                if baseline.established
                else ("provisional" if baseline.usable else "refused")
            )
            details: dict[str, Any] = {
                key: value
                for key, value in baseline.as_dict().items()
                if key not in ("daily", "context")
            }
            daily = baseline.as_dict().get("daily") or []
            details["recent_daily"] = daily[-5:]
            details["scatter_baseline"] = (
                result.scatter_baselines[name].as_dict() if name in result.scatter_baselines else None
            )
            usable = state != "refused"
            level = getattr(baseline, "level_bpm", None)
            if usable and level is not None:
                summary = f"{name}: {level:.1f} bpm, {state} over {baseline.days} day(s)"
            else:
                summary = f"{name}: no baseline - {baseline.reason}"
            ctx.metric(
                "hr_anomaly_baseline",
                round(level, 1) if usable and level is not None else "no baseline",
                unit="bpm" if usable else None,
                confidence=baseline.confidence_factor if usable else None,
                labels={"context": name, "state": state},
                details=details,
                summary=summary,
            )

    def _emit_findings(
        self, ctx: PluginContext, result: DetectionResult, accepted: Iterable[Finding]
    ) -> None:
        alerted = {finding.dedup_key for finding in accepted}
        listed = list(result.findings)[:_MAX_LISTED]
        ctx.metric(
            "hr_anomaly_findings",
            len(result.findings),
            unit="findings",
            summary=(
                f"{len(result.findings)} episode(s) crossed a rule; {len(alerted)} became alerts"
            ),
            details={
                "by_type": {name: len(result.by_type(name)) for name in FINDING_TYPES},
                "thresholds": self.anomaly.as_dict(),
                "policy": self.policy(),
                "scanned": {"days": result.days_scanned, "samples": result.samples_scanned},
                "findings": [
                    finding_summary(finding, alerted=finding.dedup_key in alerted) for finding in listed
                ],
                "listed": len(listed),
            },
        )

    def _emit_suppressed(
        self, ctx: PluginContext, held: Iterable[HeldBack], result: DetectionResult | None,
        *, findings: int = 0,
    ) -> None:
        items = list(held)
        by_code: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for item in items:
            by_code[item.code] = by_code.get(item.code, 0) + 1
            by_source[item.source] = by_source.get(item.source, 0) + 1
        if items:
            summary = f"{len(items)} candidate episode(s) did not become alerts"
        elif findings:
            summary = f"nothing was held back: all {findings} finding(s) became alerts"
        else:
            summary = "nothing crossed a rule in this window, and nothing was held back"
        ctx.metric(
            "hr_anomaly_suppressed",
            len(items),
            unit="episodes",
            summary=summary,
            details={
                "by_code": by_code,
                "by_source": by_source,
                "episodes": [item.as_dict() for item in items[:_MAX_LISTED]],
                "listed": min(len(items), _MAX_LISTED),
                "policy": self.policy(),
                "rules_that_did_not_run": [note.as_dict() for note in (result.notes if result else ())],
            },
        )


# --- small helpers (module-level, so a test can quote them) ------------------
def finding_summary(finding: Finding, *, alerted: bool) -> dict[str, Any]:
    """One finding at a size that can travel.

    :meth:`Finding.as_dict` carries the whole value series and its sampled evidence; a
    metric's ``details`` ends up in a stored row and a webhook body, so this is the same
    episode without the arrays - plus ``alerted``, which is what a reader of ``--json``
    is really asking about.
    """
    return {
        "type": finding.type,
        "context": finding.context,
        "onset": finding.onset.isoformat(),
        "offset": finding.offset.isoformat(),
        "duration_minutes": finding.duration_minutes,
        "observed": round(finding.observed, 2),
        "mean": round(finding.mean, 2),
        "unit": finding.unit,
        "threshold": round(finding.threshold, 2),
        "baseline_level": round(finding.baseline_level, 2),
        "baseline_spread": round(finding.baseline_spread, 3),
        "baseline_days": finding.baseline_days,
        "baseline_established": finding.baseline_established,
        "samples": finding.samples,
        "coverage": round(finding.coverage, 4),
        "confidence": round(finding.confidence, 3),
        "heuristic": finding.heuristic,
        "dedup_key": finding.dedup_key,
        "alerted": alerted,
    }


def thin_window_reason(coverage: Coverage, minimum: float) -> str:
    """The sentence a thin window earns - said out loud, never left as a silent zero."""
    return (
        f"only {coverage.days_present} of {coverage.days_expected} days in "
        f"[{coverage.since.isoformat()}, {coverage.until.isoformat()}) had {coverage.dataset} "
        f"readings ({coverage.fraction:.0%}), below this plugin's min_window_coverage of "
        f"{minimum:g}, so nothing was analysed and nothing was alerted on. A watch left "
        "on a nightstand is not a quiet heart: widen --since or store more days."
    )


def _fraction(config: PluginConfig, key: str, default: float) -> float:
    value = config.get_float(key, default)
    if not 0.0 <= value <= 1.0:
        raise PluginConfigError(
            f"{config.plugin_name}: option {key!r} must be between 0 and 1, got {value}",
            plugin=config.plugin_name,
        )
    return value


def _duration(config: PluginConfig, key: str, default: timedelta) -> timedelta:
    """A duration option, whether it arrived as ``"36h"`` or already as a timedelta.

    The host coerces a declared ``duration`` option before ``configure()`` runs, so both
    shapes are normal here: a value from ``config.toml`` through ``--set``, or from a
    test that builds a :class:`PluginConfig` directly, is the string.
    """
    value = config.get(key, default)
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise PluginConfigError(
            f"{config.plugin_name}: option {key!r} must be a duration like '36h', got a bool",
            plugin=config.plugin_name,
        )
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    try:
        return parse_duration(str(value), context=f"{config.plugin_name}: option {key!r}")
    except Exception as exc:  # ConfigError, and anything parse_duration ever raises
        raise PluginConfigError(str(exc), plugin=config.plugin_name) from exc


def _human(delta: timedelta) -> str:
    """A duration the size of the thing it describes: ``90 min``, ``36h``, ``2d``."""
    minutes = delta.total_seconds() / 60.0
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24.0:.0f}d"


__all__ = [
    "ENGINE_OPTIONS",
    "INSPECT_NOTE",
    "NON_DIAGNOSTIC",
    "NON_DIAGNOSTIC_CODE",
    "POLICY_OPTIONS",
    "HeldBack",
    "HrAnomalyPlugin",
    "finding_summary",
    "thin_window_reason",
]
