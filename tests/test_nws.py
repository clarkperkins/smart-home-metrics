import logging
from typing import Any

import anyio
import pytest
from aiohttp import (
    ClientConnectionError,
    ClientResponseError,
    ClientTimeout,
    RequestInfo,
)
from multidict import CIMultiDict, CIMultiDictProxy
from pydantic import ValidationError
from yarl import URL

from shm.collectors import nws
from shm.collectors.nws import NwsConfig, NwsMetricCollector
from shm.metrics import SmartHomeCollector

BASE = "https://api.weather.gov"

STATION = {"properties": {"stationIdentifier": "KAUS", "name": "Austin-Bergstrom"}}

POINT = "30.2672,-97.7431"

POINTS = {
    "properties": {
        "observationStations": "https://api.weather.gov/gridpoints/EWX/156,91/stations"
    }
}

STATIONS = {
    "features": [
        {"properties": {"stationIdentifier": "KATT", "name": "Austin Camp Mabry"}},
        {"properties": {"stationIdentifier": "KAUS", "name": "Austin-Bergstrom"}},
        {"properties": {"stationIdentifier": "KEDC", "name": "Austin Executive"}},
    ]
}


def stations_url(limit: int = 1) -> str:
    return f"{POINTS['properties']['observationStations']}?limit={limit}"


def latest_url(station: str) -> str:
    return f"{BASE}/stations/{station}/observations/latest?require_qc=true"


def qv(unit: str, value: float | None, qc: str = "V") -> dict[str, Any]:
    return {"unitCode": f"wmoUnit:{unit}", "value": value, "qualityControl": qc}


def observation(**overrides: Any) -> dict[str, Any]:
    props = {
        "timestamp": "2026-09-26T15:53:00+00:00",
        "temperature": qv("degC", 25.0),
        "dewpoint": qv("degC", 10.0),
        "windDirection": qv("degree_(angle)", 180),
        "windSpeed": qv("km_h-1", 16.09344),
        "windGust": qv("km_h-1", None),
        "barometricPressure": qv("Pa", 101592.0),
        "seaLevelPressure": qv("Pa", None),
        "visibility": qv("m", 16090),
        "precipitationLastHour": qv("mm", 25.4),
        "relativeHumidity": qv("percent", 38.5),
        "windChill": qv("degC", None),
        "heatIndex": qv("degC", None),
    }
    props.update(overrides)
    return {"properties": props}


OBSERVATION = observation()


class FakeResponse:
    def __init__(self, url: str, data: Any):
        self.url = url
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.data is None:
            # Unrouted URLs look like a network failure, not an HTTP status
            raise ClientConnectionError("connection refused")
        if isinstance(self.data, int):
            url = URL(self.url)
            info = RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)
            raise ClientResponseError(info, (), status=self.data, message="Error")

    async def json(self, content_type=None):
        return self.data


class FakeSession:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.requests: list[str] = []

    def get(self, url: str, headers=None, timeout=None):
        assert headers and "User-Agent" in headers
        assert isinstance(timeout, ClientTimeout) and timeout.total
        self.requests.append(url)
        return FakeResponse(url, self.routes.get(url))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def samples(metrics) -> dict[tuple[str, str], float]:
    return {
        (s.labels["station_id"], s.name): s.value for m in metrics for s in m.samples
    }


def station(station_id: str, name: str) -> dict[str, Any]:
    return {"properties": {"stationIdentifier": station_id, "name": name}}


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for var in (
        "NWS_STATIONS",
        "NWS_LATITUDE",
        "NWS_LONGITUDE",
        "NWS_NEAREST_STATIONS",
        "NWS_TIMEOUT",
        "NWS_USER_AGENT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(nws.time, "monotonic", c)
    return c


def set_point(monkeypatch):
    monkeypatch.setenv("NWS_LATITUDE", "30.26715")
    monkeypatch.setenv("NWS_LONGITUDE", "-97.74306")


@pytest.mark.asyncio
async def test_collect_by_station(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    session = FakeSession(
        {
            f"{BASE}/stations/KAUS": STATION,
            latest_url("KAUS"): OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    metrics = list(await collector.perform_collection())
    values = {name: v for (_, name), v in samples(metrics).items()}

    assert values["nws_temperature_f"] == pytest.approx(77.0)
    assert values["nws_dewpoint_f"] == pytest.approx(50.0)
    assert values["nws_humidity_pct"] == pytest.approx(38.5)
    assert values["nws_wind_speed_mph"] == pytest.approx(10.0)
    assert values["nws_wind_direction_degree"] == pytest.approx(180)
    assert values["nws_barometric_pressure_inhg"] == pytest.approx(30.0, abs=0.01)
    assert values["nws_visibility_mi"] == pytest.approx(10.0, abs=0.01)
    assert values["nws_precipitation_last_hour_in"] == pytest.approx(1.0)
    assert values["nws_observation_timestamp_seconds"] == 1790437980.0

    # measurements are stamped with the observation time, the staleness gauge isn't
    stamped = {s.name: s.timestamp for m in metrics for s in m.samples if s.timestamp}
    assert stamped["nws_temperature_f"] == 1790437980.0
    assert "nws_observation_timestamp_seconds" not in stamped

    # null values are omitted rather than exported as NaN/0
    assert "nws_wind_gust_mph" not in values
    assert "nws_heat_index_f" not in values

    labels = metrics[0].samples[0].labels
    assert labels == {"station_id": "KAUS", "station_name": "Austin-Bergstrom"}

    # station lookup is cached across collections
    await collector.perform_collection()
    assert session.requests.count(f"{BASE}/stations/KAUS") == 1


@pytest.mark.asyncio
async def test_units_and_quality_control(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    session = FakeSession(
        {
            f"{BASE}/stations/KAUS": STATION,
            latest_url("KAUS"): observation(
                # rejected by QC
                temperature=qv("degC", 60.0, qc="X"),
                # questionable values are still exported
                dewpoint=qv("degC", 10.0, qc="Q"),
                # the target unit depends on the field, not the source unit
                precipitationLastHour={"unitCode": "unit:m", "value": 0.0254},
                barometricPressure=qv("hPa", 1015.92),
                # wrong dimension for the field
                visibility=qv("degC", 5.0),
            ),
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    values = {
        name: v
        for (_, name), v in samples(await collector.perform_collection()).items()
    }

    assert "nws_temperature_f" not in values
    assert values["nws_dewpoint_f"] == pytest.approx(50.0)
    assert values["nws_precipitation_last_hour_in"] == pytest.approx(1.0)
    assert values["nws_barometric_pressure_inhg"] == pytest.approx(30.0, abs=0.01)
    assert not any(name.startswith("nws_visibility") for name in values)


@pytest.mark.asyncio
async def test_multiple_stations_isolate_failures(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "kaus, KATT ,KBAD,KEDC,KAUS")
    session = FakeSession(
        {
            f"{BASE}/stations/KAUS": STATION,
            f"{BASE}/stations/KATT": station("KATT", "Austin Camp Mabry"),
            f"{BASE}/stations/KEDC": station("KEDC", "Austin Executive"),
            latest_url("KAUS"): OBSERVATION,
            latest_url("KATT"): OBSERVATION,
            # KBAD fails transiently, KEDC resolves but its observation fails
            f"{BASE}/stations/KBAD": 503,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]
    assert collector.config.stations == ["KAUS", "KATT", "KBAD", "KEDC"]

    values = samples(await collector.perform_collection())

    assert ("KAUS", "nws_temperature_f") in values
    assert ("KATT", "nws_temperature_f") in values
    assert {station_id for station_id, _ in values} == {"KAUS", "KATT"}

    # unresolved stations are retried on the next scrape, resolved ones aren't
    await collector.perform_collection()
    assert session.requests.count(f"{BASE}/stations/KBAD") == 2
    assert session.requests.count(f"{BASE}/stations/KATT") == 1
    assert session.requests.count(f"{BASE}/stations/KAUS") == 1


@pytest.mark.asyncio
async def test_configured_id_differs_from_station_identifier(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "OLDID")
    session = FakeSession(
        {
            f"{BASE}/stations/OLDID": station("NEWID", "Renamed"),
            latest_url("NEWID"): OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    await collector.perform_collection()
    values = samples(await collector.perform_collection())

    assert ("NEWID", "nws_temperature_f") in values
    assert session.requests.count(f"{BASE}/stations/OLDID") == 1


@pytest.mark.asyncio
async def test_resolve_nearest_stations_from_point(monkeypatch):
    set_point(monkeypatch)
    monkeypatch.setenv("NWS_NEAREST_STATIONS", "2")
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    session = FakeSession(
        {
            f"{BASE}/points/{POINT}": POINTS,
            stations_url(2): STATIONS,
            f"{BASE}/stations/KAUS": STATION,
            latest_url("KATT"): OBSERVATION,
            latest_url("KAUS"): OBSERVATION,
            latest_url("KEDC"): OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    values = samples(await collector.perform_collection())

    # nearest 2 from the point, deduplicated against the explicit KAUS
    assert {station_id for station_id, _ in values} == {"KATT", "KAUS"}
    assert session.requests.count(latest_url("KAUS")) == 1

    await collector.perform_collection()
    assert session.requests.count(f"{BASE}/points/{POINT}") == 1


@pytest.mark.asyncio
async def test_point_is_re_resolved_daily(monkeypatch, clock):
    set_point(monkeypatch)
    routes: dict[str, Any] = {
        f"{BASE}/points/{POINT}": POINTS,
        stations_url(): STATIONS,
        latest_url("KATT"): OBSERVATION,
        latest_url("KAUS"): OBSERVATION,
    }
    session = FakeSession(routes)
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    assert {s for s, _ in samples(await collector.perform_collection())} == {"KATT"}

    # KATT is retired, so the next closest station becomes the nearest
    routes[stations_url()] = {"features": STATIONS["features"][1:]}
    clock.now += nws.POINT_TTL_SECONDS - 1
    assert {s for s, _ in samples(await collector.perform_collection())} == {"KATT"}

    clock.now += 1
    assert {s for s, _ in samples(await collector.perform_collection())} == {"KAUS"}
    assert session.requests.count(f"{BASE}/points/{POINT}") == 2


@pytest.mark.asyncio
async def test_stations_url_failure_is_retried(monkeypatch, clock):
    set_point(monkeypatch)
    routes: dict[str, Any] = {
        f"{BASE}/points/{POINT}": POINTS,
        # the point is valid, the gridpoint endpoint is just flaky
        stations_url(): 404,
        latest_url("KATT"): OBSERVATION,
    }
    session = FakeSession(routes)
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    assert not samples(await collector.perform_collection())

    routes[stations_url()] = STATIONS
    assert {s for s, _ in samples(await collector.perform_collection())} == {"KATT"}


@pytest.mark.asyncio
async def test_invalid_is_retried_after_backoff(monkeypatch, clock):
    monkeypatch.setenv("NWS_STATIONS", "KBNA,KZZZ,KRETRY,KSLOW,KBLOCK")
    monkeypatch.setenv("NWS_LATITUDE", "51.5072")
    monkeypatch.setenv("NWS_LONGITUDE", "-0.1276")
    session = FakeSession(
        {
            f"{BASE}/stations/KBNA": station("KBNA", "Nashville International"),
            latest_url("KBNA"): OBSERVATION,
            f"{BASE}/stations/KZZZ": 404,
            f"{BASE}/stations/KRETRY": 503,
            f"{BASE}/stations/KSLOW": 429,
            # e.g. the CDN blocking a user agent
            f"{BASE}/stations/KBLOCK": 403,
            # outside NWS coverage
            f"{BASE}/points/51.5072,-0.1276": 404,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    values = samples(await collector.perform_collection())
    await collector.perform_collection()

    assert {station_id for station_id, _ in values} == {"KBNA"}
    # not found means the config is probably wrong, so back off
    assert session.requests.count(f"{BASE}/stations/KZZZ") == 1
    assert session.requests.count(f"{BASE}/points/51.5072,-0.1276") == 1
    # everything else is transient
    assert session.requests.count(f"{BASE}/stations/KRETRY") == 2
    assert session.requests.count(f"{BASE}/stations/KSLOW") == 2
    assert session.requests.count(f"{BASE}/stations/KBLOCK") == 2

    # ...but not forever
    clock.now += nws.INVALID_RETRY_SECONDS
    await collector.perform_collection()
    assert session.requests.count(f"{BASE}/stations/KZZZ") == 2
    assert session.requests.count(f"{BASE}/points/51.5072,-0.1276") == 2


@pytest.mark.asyncio
async def test_location_is_not_logged(monkeypatch, caplog):
    set_point(monkeypatch)
    session = FakeSession(
        {
            f"{BASE}/points/{POINT}": 404,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    with caplog.at_level(logging.DEBUG):
        await collector.perform_collection()
        collector.point_expires = 0
        session.routes[f"{BASE}/points/{POINT}"] = 500
        await collector.perform_collection()
        collector.point_expires = 0
        session.routes[f"{BASE}/points/{POINT}"] = POINTS
        session.routes[stations_url()] = 404
        await collector.perform_collection()

    assert caplog.records
    for fragment in ("30.2", "97.7", "gridpoints", "156,91"):
        assert fragment not in caplog.text


@pytest.mark.asyncio
async def test_overlapping_collections_do_not_mix(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS,KATT")
    session = FakeSession(
        {
            f"{BASE}/stations/KAUS": STATION,
            f"{BASE}/stations/KATT": station("KATT", "Austin Camp Mabry"),
            latest_url("KAUS"): OBSERVATION,
            latest_url("KATT"): OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]
    results: list[list] = []

    async def collect():
        results.append(list(await collector.perform_collection()))

    async with anyio.create_task_group() as group:
        group.start_soon(collect)
        group.start_soon(collect)

    for metrics in results:
        temps = [s for m in metrics for s in m.samples if s.name == "nws_temperature_f"]
        assert len(temps) == 2


def test_config_validation(monkeypatch):
    monkeypatch.setenv("NWS_TIMEOUT", "12")
    with pytest.raises(ValidationError):
        NwsConfig()

    monkeypatch.delenv("NWS_TIMEOUT")
    monkeypatch.setenv("NWS_LATITUDE", "30.2")
    with pytest.raises(ValidationError):
        NwsConfig()


def test_requires_location():
    with pytest.raises(ValueError):
        NwsMetricCollector(FakeSession({}))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "env,enabled",
    [
        ({}, False),
        ({"NWS_STATIONS": ","}, False),
        # partial config skips NWS instead of failing startup
        ({"NWS_LATITUDE": "30.2"}, False),
        ({"NWS_LONGITUDE": "-97.7"}, False),
        ({"NWS_TIMEOUT": "60", "NWS_STATIONS": "KAUS"}, False),
        # pydantic-settings reads env vars case-insensitively
        ({"nws_stations": "KAUS"}, True),
        ({"NWS_LATITUDE": "30.2", "NWS_LONGITUDE": "-97.7"}, True),
    ],
)
@pytest.mark.asyncio
async def test_setup_nws(monkeypatch, caplog, env, enabled):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    collector = SmartHomeCollector()
    try:
        with caplog.at_level(logging.INFO):
            collector._setup_nws()  # pylint: disable=protected-access
    finally:
        await collector.session.close()

    assert (
        any(isinstance(c, NwsMetricCollector) for c in collector.collectors) == enabled
    )
    # validation errors don't echo the configured location
    assert "30.2" not in caplog.text
    assert "97.7" not in caplog.text
