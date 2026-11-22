"""The resolved plugin set: what was found, what works, what is broken and why.

:class:`PluginRegistry.discover` is the only entry point. Its job is to never
raise. Every candidate either becomes a usable :class:`PluginRecord` or a record
carrying the error, so ``fm plugins list`` can show a plugin that failed to import
next to one that works, and one bad file can never take the command - or an
import run - down with it.

Name collisions are resolved, not guessed. Precedence, highest first:

1. ``directory`` candidates - the user's local drop-in wins, so a one-file plugin
   can override an installed one while developing it;
2. ``entry_point`` candidates - installed distributions;
3. ``builtin`` candidates - what ships with fitbit-monitor.

Within one kind, the first candidate found wins (directories are searched in
configured order). A loser is not silently dropped: it is listed under
``collisions`` and printed by ``fm plugins list`` so the shadowing is visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import AppConfig
from .contract import MonitorPlugin
from .errors import PluginError
from .loader import (
    Candidate,
    coerce_classes,
    iter_builtin_candidates,
    iter_directory_candidates,
    iter_entry_point_candidates,
)

#: Highest precedence first. See the module docstring.
SOURCE_PRECEDENCE: tuple[str, ...] = ("directory", "entry_point", "builtin")


@dataclass
class PluginRecord:
    """One plugin, loaded or failed."""

    name: str
    kind: str  # "directory" | "entry_point"
    location: str
    plugin_class: type[MonitorPlugin] | None = None
    error: PluginError | None = None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.plugin_class is not None

    @property
    def source(self) -> str:
        """What ``fm plugins list`` prints in the SOURCE column."""
        return self.kind

    def create(self) -> MonitorPlugin:
        if self.plugin_class is None:
            raise self.error or PluginError(f"plugin {self.name} is unavailable", plugin=self.name)
        return self.plugin_class()

    def describe(self) -> dict[str, Any]:
        """The JSON view of one plugin, healthy or broken.

        The keys a consumer can always expect are name, source, location, version and
        datasets: a plugin that never loaded reports them as unknown rather than
        omitting them, so `fm plugins list --json` can be iterated blindly.
        """
        out: dict[str, Any] = {
            "name": self.name,
            "source": self.kind,
            "location": self.location,
        }
        if self.plugin_class is not None:
            out.update(self.plugin_class.describe())
        else:
            out["version"] = None
            out["datasets"] = []
        if self.error is not None:
            error: dict[str, Any] = {
                "stage": self.error.stage,
                "type": type(self.error).__name__,
                "message": self.error.message,
            }
            if self.error.hint:
                error["hint"] = self.error.hint
            out["error"] = error
        if self.note:
            out["note"] = self.note
        return out

    def row(self) -> tuple[str, str, str, str]:
        """The (name, version, source, datasets) tuple for the table renderer."""
        cls = self.plugin_class
        return (
            self.name,
            (cls.version if cls else "-"),
            self.kind,
            (", ".join(cls.datasets) if cls and cls.datasets else "-"),
        )


@dataclass
class Collision:
    """A plugin name claimed by two candidates."""

    name: str
    winner: str
    loser: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "winner": self.winner, "loser": self.loser, "reason": self.reason}


@dataclass
class PluginRegistry:
    """The ordered, resolved set of plugins."""

    records: list[PluginRecord] = field(default_factory=list)
    collisions: list[Collision] = field(default_factory=list)

    # --- construction -------------------------------------------------------
    @classmethod
    def discover(
        cls,
        config: AppConfig | None = None,
        *,
        directories: Iterable[Path | str] = (),
        include_entry_points: bool = True,
        candidates: Sequence[Candidate] | None = None,
    ) -> "PluginRegistry":
        """Find every plugin from the configured sources. Never raises.

        ``candidates`` replaces the whole discovery step - the test seam, and how a
        caller can run an ad-hoc set of classes.
        """
        cfg = config or AppConfig.load(missing_ok=True)
        extra_dirs: list[Path] = [Path(d).expanduser() for d in directories]
        if candidates is None:
            found: list[Candidate] = []
            if include_entry_points:
                found.extend(iter_builtin_candidates())
                found.extend(iter_entry_point_candidates())
            found.extend(iter_directory_candidates(list(cfg.plugin_dirs) + extra_dirs))
        else:
            found = list(candidates)
        return cls._resolve(found)

    @classmethod
    def _resolve(cls, candidates: Sequence[Candidate]) -> "PluginRegistry":
        winners: dict[str, tuple[int, PluginRecord]] = {}
        losers: list[Collision] = []
        for candidate in candidates:
            rank = _rank(candidate.kind)
            for record in _records_from_candidate(candidate):
                _claim(winners, losers, record, rank)
        ordered = [winners[name][1] for name in sorted(winners)]
        return cls(records=ordered, collisions=sorted(losers, key=lambda c: c.name))

    # --- queries ------------------------------------------------------------
    def names(self) -> list[str]:
        return [r.name for r in self.records]

    def get(self, name: str) -> PluginRecord | None:
        for record in self.records:
            if record.name == name:
                return record
        return None

    def __contains__(self, name: object) -> bool:
        return any(r.name == name for r in self.records)

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def errors(self) -> list[PluginRecord]:
        return [r for r in self.records if not r.ok]

    def usable(self) -> list[PluginRecord]:
        return [r for r in self.records if r.ok]

    def instantiate(self, name: str) -> tuple[MonitorPlugin, PluginRecord]:
        record = self.get(name)
        if record is None:
            raise PluginError(f"no plugin named {name!r}", plugin=name, hint="see `fm plugins list`")
        return record.create(), record

    def as_dicts(self) -> list[dict[str, Any]]:
        return [r.describe() for r in self.records]


def _rank(kind: str) -> int:
    """Lower is higher precedence."""
    try:
        return SOURCE_PRECEDENCE.index(kind)
    except ValueError:
        return len(SOURCE_PRECEDENCE)


def _label(candidate: Candidate) -> str:
    return f"{candidate.kind}:{candidate.location}"


def _records_for(cls_: type[MonitorPlugin], candidate: Candidate) -> list[PluginRecord]:
    return [PluginRecord(name=cls_.name, kind=candidate.kind, location=candidate.location, plugin_class=cls_)]


def _records_from_candidate(candidate: Candidate) -> list[PluginRecord]:
    """Load one candidate into zero or more records. A failure is a record, never an exception."""
    try:
        classes = coerce_classes(candidate.load(), label=_label(candidate))
    except PluginError as exc:
        return [PluginRecord(name=candidate.label, kind=candidate.kind, location=candidate.location, error=exc)]
    except BaseException as exc:  # noqa: BLE001 - a plugin must never break the host
        return [
            PluginRecord(
                name=candidate.label,
                kind=candidate.kind,
                location=candidate.location,
                error=PluginError(
                    f"unexpected {type(exc).__name__} while discovering {candidate.label}: {exc}",
                    plugin=candidate.label,
                    hint="run `fm plugins validate -v` for the traceback",
                ),
            )
        ]
    if not classes:
        return [
            PluginRecord(
                name=candidate.label,
                kind=candidate.kind,
                location=candidate.location,
                error=PluginError(
                    f"{candidate.location} produced no plugin class", plugin=candidate.label
                ),
            )
        ]
    return [PluginRecord(name=c.name, kind=candidate.kind, location=candidate.location, plugin_class=c) for c in classes]




def _claim(
    winners: dict[str, tuple[int, PluginRecord]],
    losers: list[Collision],
    record: PluginRecord,
    rank: int,
) -> None:
    current = winners.get(record.name)
    if current is not None and current[1].plugin_class is record.plugin_class is not None:
        # The built-ins are also published as entry points of this distribution;
        # the same class found twice is not a collision.
        return
    if current is None:
        winners[record.name] = (rank, record)
        return
    current_rank, current_record = current
    if rank < current_rank:
        winners[record.name] = (rank, record)
        losers.append(
            Collision(
                name=record.name,
                winner=record.location,
                loser=current_record.location,
                reason=f"{record.kind} outranks {current_record.kind}",
            )
        )
    else:
        losers.append(
            Collision(
                name=record.name,
                winner=current_record.location,
                loser=record.location,
                reason=f"{current_record.kind} already claimed this name",
            )
        )


__all__ = ["Collision", "PluginRecord", "PluginRegistry", "SOURCE_PRECEDENCE"]
