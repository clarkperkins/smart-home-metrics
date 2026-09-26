from typing import Any

import pytest

from shm.collectors.nws import NwsMetricCollector

BASE = "https://api.weather.gov"

STATION = {"properties": {"stationIdentifier": "KAUS", "name": "Austin-Bergstrom"}}

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


def qv(unit: str, value: float | None) -> dict[str, Any]:
    return {"unitCode": f"wmoUnit:{unit}", "value": value, "qualityControl": "V"}


OBSERVATION = {
    "properties": {
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
}


class FakeResponse:
    def __init__(self, data: Any):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.data is None:
            raise RuntimeError("404")

    async def json(self, content_type=None):
        return self.data


class FakeSession:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.requests: list[str] = []

    def get(self, url: str, headers=None):
        assert headers and "User-Agent" in headers
        self.requests.append(url)
        return FakeResponse(self.routes.get(url))


def samples(metrics) -> dict[tuple[str, str], float]:
    return {
        (s.labels["station_id"], s.name): s.value for m in metrics for s in m.samples
    }


def station(station_id: str, name: str) -> dict[str, Any]:
    return {"properties": {"stationIdentifier": station_id, "name": name}}


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for var in ("NWS_STATIONS", "NWS_LATITUDE", "NWS_LONGITUDE"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
async def test_collect_by_station(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    session = FakeSession(
        {
            f"{BASE}/stations/KAUS": STATION,
            f"{BASE}/stations/KAUS/observations/latest": OBSERVATION,
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

    # null values are omitted rather than exported as NaN/0
    assert "nws_wind_gust_mph" not in values
    assert "nws_heat_index_f" not in values

    labels = metrics[0].samples[0].labels
    assert labels == {"station_id": "KAUS", "station_name": "Austin-Bergstrom"}

    # station lookup is cached across collections
    await collector.perform_collection()
    assert session.requests.count(f"{BASE}/stations/KAUS") == 1


@pytest.mark.asyncio
async def test_multiple_stations_isolate_failures(monkeypatch):
    monkeypatch.setenv("NWS_STATIONS", "kaus, KATT ,KBAD,KEDC")
    session = FakeSession(
        {
            f"{BASE}/stations/KAUS": STATION,
            f"{BASE}/stations/KATT": station("KATT", "Austin Camp Mabry"),
            f"{BASE}/stations/KEDC": station("KEDC", "Austin Executive"),
            f"{BASE}/stations/KAUS/observations/latest": OBSERVATION,
            f"{BASE}/stations/KATT/observations/latest": OBSERVATION,
            # KBAD fails to resolve, KEDC resolves but its observation fails
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    values = samples(await collector.perform_collection())

    assert ("KAUS", "nws_temperature_f") in values
    assert ("KATT", "nws_temperature_f") in values
    assert {station_id for station_id, _ in values} == {"KAUS", "KATT"}

    # unresolved stations are retried on the next scrape, resolved ones aren't
    await collector.perform_collection()
    assert session.requests.count(f"{BASE}/stations/KBAD") == 2
    assert session.requests.count(f"{BASE}/stations/KATT") == 1


@pytest.mark.asyncio
async def test_resolve_nearest_stations_from_point(monkeypatch):
    monkeypatch.setenv("NWS_LATITUDE", "30.26715")
    monkeypatch.setenv("NWS_LONGITUDE", "-97.74306")
    monkeypatch.setenv("NWS_NEAREST_STATIONS", "2")
    monkeypatch.setenv("NWS_STATIONS", "KAUS")
    session = FakeSession(
        {
            f"{BASE}/points/30.2672,-97.7431": POINTS,
            POINTS["properties"]["observationStations"]: STATIONS,
            f"{BASE}/stations/KAUS": STATION,
            f"{BASE}/stations/KATT/observations/latest": OBSERVATION,
            f"{BASE}/stations/KAUS/observations/latest": OBSERVATION,
            f"{BASE}/stations/KEDC/observations/latest": OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    values = samples(await collector.perform_collection())

    # nearest 2 from the point, deduplicated against the explicit KAUS
    assert {station_id for station_id, _ in values} == {"KATT", "KAUS"}
    assert session.requests.count(f"{BASE}/stations/KAUS/observations/latest") == 1

    await collector.perform_collection()
    assert session.requests.count(f"{BASE}/points/30.2672,-97.7431") == 1


def test_requires_location():
    with pytest.raises(ValueError):
        NwsMetricCollector(FakeSession({}))  # type: ignore[arg-type]
