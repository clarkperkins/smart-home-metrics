import logging
from datetime import UTC, datetime
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


def obs_url(station: str) -> str:
    # FakeSession ignores the ?start= query, which depends on the time
    return f"{BASE}/stations/{station}/observations"


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
        path, _, query = url.partition("?")
        if path.endswith("/observations") and query.startswith("start="):
            data = self.routes.get(path)
            # A single observation route is served as a one-record history
            if isinstance(data, dict) and "properties" in data:
                data = {"features": [data]}
            return FakeResponse(url, data)
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


# Shortly after the default observation time
NOW = datetime(2026, 9, 26, 16, 7, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def now(monkeypatch) -> list[datetime]:
    current = [NOW]
    monkeypatch.setattr(nws, "_now", lambda: current[0])
    return current


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
            obs_url("KAUS"): OBSERVATION,
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
            obs_url("KAUS"): observation(
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
            obs_url("KAUS"): OBSERVATION,
            obs_url("KATT"): OBSERVATION,
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
            obs_url("NEWID"): OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    values = samples(await collector.perform_collection())
    await collector.perform_collection()

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
            obs_url("KATT"): OBSERVATION,
            obs_url("KAUS"): OBSERVATION,
            obs_url("KEDC"): OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    values = samples(await collector.perform_collection())

    # nearest 2 from the point, deduplicated against the explicit KAUS
    assert {station_id for station_id, _ in values} == {"KATT", "KAUS"}
    assert sum(u.startswith(obs_url("KAUS") + "?") for u in session.requests) == 1

    await collector.perform_collection()
    assert session.requests.count(f"{BASE}/points/{POINT}") == 1


@pytest.mark.asyncio
async def test_point_is_re_resolved_daily(monkeypatch, clock):
    set_point(monkeypatch)
    routes: dict[str, Any] = {
        f"{BASE}/points/{POINT}": POINTS,
        stations_url(): STATIONS,
        obs_url("KATT"): OBSERVATION,
        obs_url("KAUS"): OBSERVATION,
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
        obs_url("KATT"): OBSERVATION,
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
            obs_url("KBNA"): OBSERVATION,
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
            obs_url("KAUS"): OBSERVATION,
            obs_url("KATT"): OBSERVATION,
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
        stamps = [
            s
            for m in metrics
            for s in m.samples
            if s.name.endswith("_timestamp_seconds")
        ]
        assert len(stamps) == 2

    # each observation is exported once, by whichever collection ran first
    temps = [
        s.labels["station_id"]
        for metrics in results
        for m in metrics
        for s in m.samples
        if s.name == "nws_temperature_f"
    ]
    assert sorted(temps) == ["KATT", "KAUS"]


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


def history(*observations: dict[str, Any]) -> dict[str, Any]:
    return {"features": list(observations)}


def at(ts: str, **overrides: Any) -> dict[str, Any]:
    return observation(timestamp=f"2026-09-26T{ts}:00+00:00", **overrides)


def pending(ts: str, **overrides: Any) -> dict[str, Any]:
    # How mesonet stations publish a record before QC fills it in
    fields = {
        field: qv(unit, None, qc="Z")
        for field, unit in [
            ("temperature", "degC"),
            ("dewpoint", "degC"),
            ("windDirection", "degree_(angle)"),
            ("windSpeed", "km_h-1"),
            ("windGust", "km_h-1"),
            ("barometricPressure", "Pa"),
            ("seaLevelPressure", "Pa"),
            ("visibility", "m"),
            ("precipitationLastHour", "mm"),
            ("relativeHumidity", "percent"),
            ("windChill", "degC"),
            ("heatIndex", "degC"),
        ]
    }
    return at(ts, **{**fields, **overrides})


def temperatures(metrics) -> list[tuple[float | None, float]]:
    return [
        (s.timestamp, s.value)
        for m in metrics
        for s in m.samples
        if s.name == "nws_temperature_f"
    ]


def ts(hhmm: str) -> float:
    return datetime.fromisoformat(f"2026-09-26T{hhmm}:00+00:00").timestamp()


@pytest.mark.asyncio
async def test_exports_each_qcd_observation_once(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    routes: dict[str, Any] = {
        f"{BASE}/stations/KAUS": STATION,
        # the API returns newest first
        obs_url("KAUS"): history(
            at("15:50", temperature=qv("degC", 20.0)),
            at("15:45", temperature=qv("degC", 15.0)),
            at("15:40", temperature=qv("degC", 10.0)),
        ),
    }
    session = FakeSession(routes)
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    metrics = list(await collector.perform_collection())
    # every observation, oldest first, each at its own time
    assert temperatures(metrics) == [
        (ts("15:40"), pytest.approx(50.0)),
        (ts("15:45"), pytest.approx(59.0)),
        (ts("15:50"), pytest.approx(68.0)),
    ]
    assert samples(metrics)[("KAUS", "nws_observation_timestamp_seconds")] == ts(
        "15:50"
    )

    # nothing new: no measurements, but the staleness gauge stays
    metrics = list(await collector.perform_collection())
    assert not temperatures(metrics)
    assert samples(metrics)[("KAUS", "nws_observation_timestamp_seconds")] == ts(
        "15:50"
    )

    # only the new observation is exported
    routes[obs_url("KAUS")]["features"].insert(
        0, at("15:55", temperature=qv("degC", 25.0))
    )
    metrics = list(await collector.perform_collection())
    assert temperatures(metrics) == [(ts("15:55"), pytest.approx(77.0))]

    # the start of the window is rounded down to 5 minutes, an hour back
    assert session.requests[-1] == f"{obs_url('KAUS')}?start=2026-09-26T15:05:00Z"


@pytest.mark.asyncio
async def test_waits_for_qc_before_exporting_newer_observations(monkeypatch, now):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    routes: dict[str, Any] = {
        f"{BASE}/stations/KAUS": STATION,
        obs_url("KAUS"): history(
            at("16:00"),
            pending("15:55"),
            at("15:50"),
        ),
    }
    collector = NwsMetricCollector(FakeSession(routes))  # type: ignore[arg-type]

    # 15:55 is still waiting on QC, so 16:00 is held back
    metrics = list(await collector.perform_collection())
    assert [t for t, _ in temperatures(metrics)] == [ts("15:50")]
    assert samples(metrics)[("KAUS", "nws_observation_timestamp_seconds")] == ts(
        "15:50"
    )

    # once it's QC'd, both are exported in order
    routes[obs_url("KAUS")] = history(at("16:00"), at("15:55"), at("15:50"))
    metrics = list(await collector.perform_collection())
    assert [t for t, _ in temperatures(metrics)] == [ts("15:55"), ts("16:00")]

    # a record that never gets QC'd stops holding back newer ones after the grace
    routes[obs_url("KAUS")] = history(at("16:10"), pending("16:05"), at("16:00"))
    assert not temperatures(await collector.perform_collection())
    now[0] = datetime.fromisoformat("2026-09-26T16:05:00+00:00") + nws.PENDING_GRACE
    metrics = list(await collector.perform_collection())
    assert [t for t, _ in temperatures(metrics)] == [ts("16:10")]


@pytest.mark.asyncio
async def test_partially_pending_observation_counts_as_qcd(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    session = FakeSession(
        {
            f"{BASE}/stations/KAUS": STATION,
            obs_url("KAUS"): history(
                at("16:00"),
                # e.g. a METAR whose dewpoint never gets QC'd
                at("15:55", dewpoint=qv("degC", None, qc="Z")),
            ),
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    metrics = list(await collector.perform_collection())
    assert [t for t, _ in temperatures(metrics)] == [ts("15:55"), ts("16:00")]


@pytest.mark.asyncio
async def test_no_qcd_observations_yet(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    session = FakeSession(
        {
            f"{BASE}/stations/KAUS": STATION,
            obs_url("KAUS"): history(pending("16:05")),
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    assert not list(await collector.perform_collection())


def dewpoints(metrics) -> list[float | None]:
    return [
        s.timestamp for m in metrics for s in m.samples if s.name == "nws_dewpoint_f"
    ]


@pytest.mark.asyncio
async def test_field_filled_in_later_is_still_exported(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    routes: dict[str, Any] = {
        f"{BASE}/stations/KAUS": STATION,
        obs_url("KAUS"): history(
            at("16:00", dewpoint=qv("degC", None, qc="Z")),
            at("15:55"),
        ),
    }
    collector = NwsMetricCollector(FakeSession(routes))  # type: ignore[arg-type]

    # the 16:00 record is QC'd apart from its dewpoint
    metrics = list(await collector.perform_collection())
    assert [t for t, _ in temperatures(metrics)] == [ts("15:55"), ts("16:00")]
    assert dewpoints(metrics) == [ts("15:55")]

    # once the dewpoint fills in it's exported, without re-exporting the temperature
    routes[obs_url("KAUS")] = history(at("16:00"), at("15:55"))
    metrics = list(await collector.perform_collection())
    assert not temperatures(metrics)
    assert dewpoints(metrics) == [ts("16:00")]


@pytest.mark.asyncio
async def test_screened_wind_gust_does_not_count_as_qcd(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    routes: dict[str, Any] = {
        f"{BASE}/stations/KAUS": STATION,
        obs_url("KAUS"): history(
            at("16:00"),
            # mesonet stations publish the gust before QC reaches the rest
            pending("15:55", windGust=qv("km_h-1", 20.0, qc="S")),
            at("15:50"),
        ),
    }
    collector = NwsMetricCollector(FakeSession(routes))  # type: ignore[arg-type]

    # 15:55 is still pending, so 16:00 is held back rather than exported ahead of it
    metrics = list(await collector.perform_collection())
    assert [t for t, _ in temperatures(metrics)] == [ts("15:50")]

    routes[obs_url("KAUS")] = history(at("16:00"), at("15:55"), at("15:50"))
    metrics = list(await collector.perform_collection())
    assert [t for t, _ in temperatures(metrics)] == [ts("15:55"), ts("16:00")]
