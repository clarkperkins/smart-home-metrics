from types import SimpleNamespace
from typing import Any

import pytest
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


async def collect(c: EcobeeMetricCollector) -> dict[tuple[str, tuple], float]:
    samples = {}
    for metric in await c.perform_collection():
        for sample in metric.samples:
            labels = {k: v for k, v in sample.labels.items() if k not in LABELS}
            samples[(sample.name, tuple(sorted(labels.items())))] = sample.value
    return samples


def runtime_seconds(samples: dict[tuple[str, tuple], Any], equipment: str) -> float:
    return samples[
        ("ecobee_equipment_runtime_seconds_total", (("equipment", equipment),))
    ]


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
async def test_equipment_runtime_counts_each_interval_once(collector):
    # First readings only set the baseline
    collector.ecobee.thermostat = thermostat(
        runtime=RUNTIME,
        extended=extended_runtime(
            "2026-10-11 14:00:00", cool1=[300, 300, 300], fan=[300, 300, 300]
        ),
    )
    samples = await collect(collector)

    assert runtime_seconds(samples, "compCool1") == 0
    assert runtime_seconds(samples, "heatPump") == 0
    assert samples[("ecobee_extended_runtime_timestamp_seconds", ())] == 1791727200

    # The next 15 minutes: all 3 intervals are new
    collector.ecobee.revisions = "t1:a1:r1:i2"
    collector.ecobee.thermostat = thermostat(
        extended=extended_runtime(
            "2026-10-11 14:15:00", cool1=[120, 0, 300], fan=[300, 30, 300]
        )
    )
    samples = await collect(collector)

    assert collector.ecobee.selections[-1].include_extended_runtime
    assert runtime_seconds(samples, "compCool1") == 420
    assert runtime_seconds(samples, "fan") == 630
    assert runtime_seconds(samples, "heatPump") == 0

    # Nothing new: the cached readings aren't counted again
    collector.ecobee.thermostat = thermostat()
    samples = await collect(collector)

    assert not collector.ecobee.selections[-1].include_extended_runtime
    assert runtime_seconds(samples, "compCool1") == 420

    # Readings overlapping the last batch only count the newer intervals
    collector.ecobee.revisions = "t1:a1:r1:i3"
    collector.ecobee.thermostat = thermostat(
        extended=extended_runtime(
            "2026-10-11 14:20:00", cool1=[0, 300, 200], fan=[30, 300, 200]
        )
    )
    samples = await collect(collector)

    assert runtime_seconds(samples, "compCool1") == 620
    assert runtime_seconds(samples, "fan") == 830
    assert samples[("ecobee_extended_runtime_timestamp_seconds", ())] == 1791728400


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
