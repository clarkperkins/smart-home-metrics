from typing import Any

import pytest

from shm.collectors.nws import NwsMetricCollector

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
        pass

    async def json(self, content_type=None):
        return self.data


class FakeSession:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.requests: list[str] = []

    def get(self, url: str, headers=None):
        assert headers and "User-Agent" in headers
        self.requests.append(url)
        return FakeResponse(self.routes[url])


def samples(metrics) -> dict[str, float]:
    return {s.name: s.value for m in metrics for s in m.samples}


@pytest.mark.asyncio
async def test_collect_by_station(monkeypatch):
    monkeypatch.setenv("NWS_STATION", "KAUS")
    session = FakeSession(
        {
            "https://api.weather.gov/stations/KAUS": STATION,
            "https://api.weather.gov/stations/KAUS/observations/latest": OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    metrics = list(await collector.perform_collection())
    values = samples(metrics)

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
    assert session.requests.count("https://api.weather.gov/stations/KAUS") == 1


@pytest.mark.asyncio
async def test_resolve_station_from_point(monkeypatch):
    monkeypatch.setenv("NWS_LATITUDE", "30.26715")
    monkeypatch.setenv("NWS_LONGITUDE", "-97.74306")
    session = FakeSession(
        {
            "https://api.weather.gov/points/30.2672,-97.7431": POINTS,
            POINTS["properties"]["observationStations"]: STATIONS,
            "https://api.weather.gov/stations/KATT/observations/latest": OBSERVATION,
        }
    )
    collector = NwsMetricCollector(session)  # type: ignore[arg-type]

    metrics = list(await collector.perform_collection())

    assert metrics
    assert metrics[0].samples[0].labels["station_id"] == "KATT"


def test_requires_location(monkeypatch):
    for var in ("NWS_STATION", "NWS_LATITUDE", "NWS_LONGITUDE"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError):
        NwsMetricCollector(FakeSession({}))  # type: ignore[arg-type]
