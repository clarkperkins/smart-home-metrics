from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from prometheus_client.samples import Sample
from pyecobee import (
    ExtendedRuntime,
    Runtime,
    Selection,
    Settings,
    Thermostat,
    Weather,
    WeatherForecast,
)

from shm.collectors import ecobee
from shm.collectors.ecobee import EcobeeMetricCollector

ID = "1234"
NAME = "Upstairs"
LABELS = {"thermostat_id": ID, "thermostat_name": NAME, "sensor_name": ""}


def extended_runtime(timestamp: str, **equipment: list[int]) -> ExtendedRuntime:
    return ExtendedRuntime(last_reading_timestamp=timestamp, **equipment)


def thermostat(
    extended: ExtendedRuntime | None = None,
    settings: Settings | None = None,
    runtime: Runtime | None = None,
) -> Thermostat:
    return Thermostat(
        ID,
        name=NAME,
        equipment_status="compCool1,fan",
        runtime=runtime,
        extended_runtime=extended,
        settings=settings,
        remote_sensors=[],
        weather=Weather(
            forecasts=[
                WeatherForecast(temperature=850, relative_humidity=60, dewpoint=700)
            ]
        ),
    )


RUNTIME = Runtime(
    actual_temperature=735,
    raw_temperature=736,
    actual_humidity=52,
    desired_heat=680,
    desired_cool=740,
    desired_humidity=36,
    desired_dehumidity=55,
    actual_voc=100,
    actual_co2=600,
)

SETTINGS = Settings(
    hvac_mode="cool",
    humidifier_mode="off",
    dehumidifier_mode="on",
    dehumidify_with_a_c=True,
)


class FakeEcobee:
    """Answers with whatever thermostat and revisions the test sets next"""

    def __init__(self):
        self.revisions = "t1:a1:r1:i1"
        self.thermostat = thermostat()
        self.selections: list[Selection] = []

    async def ensure_tokens(self) -> bool:
        return True

    async def request_thermostats_summary_async(self, *_args, **_kwargs):
        return SimpleNamespace(revision_list=[f"{ID}:{NAME}:true:{self.revisions}"])

    async def request_thermostats_async(self, selection: Selection, **_kwargs):
        self.selections.append(selection)
        return SimpleNamespace(thermostat_list=[self.thermostat])


@pytest.fixture
def collector(monkeypatch) -> EcobeeMetricCollector:
    monkeypatch.setattr(ecobee, "MetricsEcobeeService", FakeEcobee)
    return EcobeeMetricCollector(session=None)  # type: ignore[arg-type]


async def collect_samples(c: EcobeeMetricCollector) -> list[Sample]:
    return [
        sample for metric in await c.perform_collection() for sample in metric.samples
    ]


async def collect(c: EcobeeMetricCollector) -> dict[tuple[str, tuple], float]:
    samples = {}
    for sample in await collect_samples(c):
        labels = {k: v for k, v in sample.labels.items() if k not in LABELS}
        samples[(sample.name, tuple(sorted(labels.items())))] = sample.value
    return samples


def runtime_points(samples: list[Sample], equipment: str) -> list[tuple[str, float]]:
    """(interval end as HH:MM UTC, total) for each sample exported, in order"""
    return [
        (
            datetime.fromtimestamp(cast(float, s.timestamp), UTC).strftime("%H:%M"),
            s.value,
        )
        for s in samples
        if s.name == "ecobee_equipment_runtime_seconds_total"
        and s.labels["equipment"] == equipment
    ]


def reading_timestamp(samples: list[Sample]) -> float:
    (sample,) = [
        s for s in samples if s.name == "ecobee_extended_runtime_timestamp_seconds"
    ]
    assert sample.timestamp is None
    return sample.value


@pytest.mark.asyncio
async def test_settings_and_humidity_setpoints(collector):
    collector.ecobee.thermostat = thermostat(runtime=RUNTIME, settings=SETTINGS)

    samples = await collect(collector)

    assert samples[("ecobee_desired_humidity_pct", ())] == 36
    assert samples[("ecobee_desired_dehumidity_pct", ())] == 55
    assert samples[("ecobee_hvac_mode", (("ecobee_hvac_mode", "cool"),))] == 1
    assert samples[("ecobee_hvac_mode", (("ecobee_hvac_mode", "heat"),))] == 0
    assert (
        samples[("ecobee_humidifier_mode", (("ecobee_humidifier_mode", "off"),))] == 1
    )
    assert (
        samples[("ecobee_dehumidifier_mode", (("ecobee_dehumidifier_mode", "on"),))]
        == 1
    )
    assert samples[("ecobee_dehumidify_with_ac", ())] == 1


@pytest.mark.asyncio
async def test_settings_kept_when_thermostat_revision_unchanged(collector):
    collector.ecobee.thermostat = thermostat(runtime=RUNTIME, settings=SETTINGS)
    await collect(collector)

    # Same revisions: ecobee only sends what was asked for, so no settings
    collector.ecobee.thermostat = thermostat()
    samples = await collect(collector)

    assert not collector.ecobee.selections[-1].include_settings
    assert samples[("ecobee_hvac_mode", (("ecobee_hvac_mode", "cool"),))] == 1
    assert samples[("ecobee_desired_humidity_pct", ())] == 36


@pytest.mark.asyncio
async def test_unknown_mode_is_its_own_state(collector):
    collector.ecobee.thermostat = thermostat(
        runtime=RUNTIME, settings=Settings(hvac_mode="emergencyHeat")
    )

    samples = await collect(collector)

    assert samples[("ecobee_hvac_mode", (("ecobee_hvac_mode", "emergencyHeat"),))] == 1
    assert samples[("ecobee_hvac_mode", (("ecobee_hvac_mode", "auto"),))] == 0


@pytest.mark.asyncio
async def test_equipment_runtime_exports_each_interval_once(collector):
    # First readings only set the baseline
    collector.ecobee.thermostat = thermostat(
        runtime=RUNTIME,
        extended=extended_runtime(
            "2026-10-11 14:00:00", cool1=[300, 300, 300], fan=[300, 300, 300]
        ),
    )
    samples = await collect_samples(collector)

    assert runtime_points(samples, "compCool1") == [("14:00", 0)]
    assert runtime_points(samples, "heatPump") == [("14:00", 0)]
    assert reading_timestamp(samples) == 1791727200

    # The next 15 minutes: all 3 intervals are new, each at its own end time
    collector.ecobee.revisions = "t1:a1:r1:i2"
    collector.ecobee.thermostat = thermostat(
        extended=extended_runtime(
            "2026-10-11 14:15:00", cool1=[120, 0, 300], fan=[300, 30, 300]
        )
    )
    samples = await collect_samples(collector)

    assert collector.ecobee.selections[-1].include_extended_runtime
    assert runtime_points(samples, "compCool1") == [
        ("14:05", 120),
        ("14:10", 120),
        ("14:15", 420),
    ]
    assert runtime_points(samples, "fan") == [
        ("14:05", 300),
        ("14:10", 330),
        ("14:15", 630),
    ]
    # No readings for it in this batch, so nothing new to export
    assert runtime_points(samples, "heatPump") == []

    # Nothing new: nothing is exported again (it would be out of order), but
    # the reading timestamp still is
    collector.ecobee.thermostat = thermostat()
    samples = await collect_samples(collector)

    assert not collector.ecobee.selections[-1].include_extended_runtime
    assert runtime_points(samples, "compCool1") == []
    assert reading_timestamp(samples) == 1791728100

    # Readings overlapping the last batch only count the newer intervals
    collector.ecobee.revisions = "t1:a1:r1:i3"
    collector.ecobee.thermostat = thermostat(
        extended=extended_runtime(
            "2026-10-11 14:20:00", cool1=[0, 300, 200], fan=[30, 300, 200]
        )
    )
    samples = await collect_samples(collector)

    assert runtime_points(samples, "compCool1") == [("14:20", 620)]
    assert runtime_points(samples, "fan") == [("14:20", 830)]
    assert reading_timestamp(samples) == 1791728400


@pytest.mark.asyncio
async def test_removed_thermostat_drops_runtime_totals(collector):
    collector.ecobee.thermostat = thermostat(
        runtime=RUNTIME,
        extended=extended_runtime("2026-10-11 14:00:00", cool1=[300, 300, 300]),
    )
    await collect(collector)

    async def no_thermostats(*_args, **_kwargs):
        return SimpleNamespace(revision_list=[])

    collector.ecobee.request_thermostats_summary_async = no_thermostats
    samples = await collect(collector)

    assert not samples
    assert ID not in collector._runtime_totals
    assert ID not in collector._runtime_counted_until
