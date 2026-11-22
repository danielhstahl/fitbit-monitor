"""What the store knows how to hold: kind, native grain, and units per dataset.

One place answers three questions that used to be implicit:

* **kind** - is this dataset one row per instants (``intraday``) or one row per day
  (``daily``)? Completeness and gap reporting differ between the two.
* **granularity_seconds** - the native spacing of the series, which is what makes
  "this break is a gap" decidable instead of a matter of opinion.
* **units** - the unit of every field, so a read-API consumer prints ``bpm`` instead
  of guessing, and so a plugin's ``MetricResult.unit`` has something to agree with.

The catalog is metadata only: it holds no data, and a dataset that is not listed here
still works (it is registered as ``daily`` with no units) so a CSV import of a column
fm has never seen does not fail.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Fields whose unit is the same wherever they appear.
FIELD_UNITS: dict[str, str] = {
    "hr": "beats per minute",
    "resting_hr": "beats per minute",
    "steps": "steps",
    "calories": "kcal",
    "active_minutes": "minutes",
    "sedentary_minutes": "minutes",
    "rmssd": "milliseconds",
    "sdnn": "milliseconds",
    "hrv_n": "samples",
    "sleep_minutes": "minutes",
    "awake_minutes": "minutes",
    "deep_minutes": "minutes",
    "light_minutes": "minutes",
    "rem_minutes": "minutes",
    "spo2_min": "percent",
    "spo2_avg": "percent",
    "body_fat_pct": "percent",
    "weight_kg": "kilograms",
}

KINDS = ("intraday", "daily")


@dataclass(frozen=True)
class DatasetSpec:
    """How one dataset is shaped. Immutable metadata, safe to hand to plugins."""

    name: str
    kind: str = "daily"
    granularity_seconds: int | None = None
    summary: str = ""

    @property
    def units(self) -> dict[str, str]:
        """Declared units for the fields this dataset carries (unknown ones are absent)."""
        return {field: FIELD_UNITS[field] for field in _FIELDS.get(self.name, ()) if field in FIELD_UNITS}

    def unit(self, field: str) -> str | None:
        return FIELD_UNITS.get(field)

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.name,
            "kind": self.kind,
            "granularity_seconds": self.granularity_seconds,
            "summary": self.summary,
            "fields": list(_FIELDS.get(self.name, ())),
            "units": self.units,
        }


#: Field lists, kept in step with the seed corpus and the API catalog (both import this).
_FIELDS: dict[str, tuple[str, ...]] = {
    "heart_rate": ("hr",),
    "daily_summary": ("resting_hr", "steps", "calories", "active_minutes"),
    "hrv": ("rmssd", "sdnn", "hrv_n"),
    "sleep": ("sleep_minutes", "awake_minutes", "deep_minutes"),
    "spo2": ("spo2_min", "spo2_avg"),
}

_SPECS: dict[str, DatasetSpec] = {
    # One value a minute is what Fitbit's intraday endpoints answer with.
    "heart_rate": DatasetSpec("heart_rate", "intraday", 60, "intraday heart rate, one row per sample"),
    "daily_summary": DatasetSpec("daily_summary", "daily", 86400, "one row per calendar day"),
    "hrv": DatasetSpec("hrv", "daily", 86400, "nightly HRV summary, one row per day"),
    "sleep": DatasetSpec("sleep", "daily", 86400, "sleep session totals, one row per night"),
    "spo2": DatasetSpec("spo2", "daily", 86400, "daily minimum/average SpO2"),
}


def spec(dataset: str) -> DatasetSpec:
    """The spec for ``dataset``; unlisted datasets are treated as daily and unitless."""
    known = _SPECS.get(dataset)
    if known is not None:
        return known
    return DatasetSpec(dataset, "daily", None, f"dataset {dataset!r} (not in the built-in catalog)")


def fields(dataset: str) -> tuple[str, ...]:
    """Declared fields for a dataset (empty for a dataset fm does not know)."""
    return _FIELDS.get(dataset, ())


def register_fields(dataset: str, names: tuple[str, ...]) -> None:
    """Teach the catalog a dataset's fields (used by the importer when it normalizes)."""
    if names and dataset not in _FIELDS:
        _FIELDS[dataset] = tuple(names)


def known() -> tuple[str, ...]:
    return tuple(_SPECS)


__all__ = ["FIELD_UNITS", "DatasetSpec", "fields", "known", "register_fields", "spec"]
