import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Self

from aiohttp import ClientSession
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from shm.collectors import MetricCollector

logger = logging.getLogger(__name__)

BASE_URL = "https://api.weather.gov"


class NwsConfig(BaseSettings):
    """
    Either `station` (e.g. KAUS) or `latitude` + `longitude` must be set.
    With coordinates, the nearest observation station is looked up from the /points API.
    """

    model_config = SettingsConfigDict(env_prefix="NWS_")

    station: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # api.weather.gov requires a User-Agent identifying the application,
    # ideally with contact info: https://www.weather.gov/documentation/services-web-api
    user_agent: str = "smart-home-metrics (github.com/clarkperkins/smart-home-metrics)"

    @model_validator(mode="after")
    def check_location(self) -> Self:
        if not self.station and (self.latitude is None or self.longitude is None):
            raise ValueError(
                "NWS_STATION or NWS_LATITUDE and NWS_LONGITUDE must be set"
            )
        return self


class QuantitativeValue(BaseModel):
    unit_code: str = Field(alias="unitCode")
    value: float | None = None


class Observation(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    timestamp: datetime
    temperature: QuantitativeValue | None = None
    dewpoint: QuantitativeValue | None = None
    wind_direction: QuantitativeValue | None = Field(None, alias="windDirection")
    wind_speed: QuantitativeValue | None = Field(None, alias="windSpeed")
    wind_gust: QuantitativeValue | None = Field(None, alias="windGust")
    barometric_pressure: QuantitativeValue | None = Field(
        None, alias="barometricPressure"
    )
    sea_level_pressure: QuantitativeValue | None = Field(None, alias="seaLevelPressure")
    visibility: QuantitativeValue | None = None
    precipitation_last_hour: QuantitativeValue | None = Field(
        None, alias="precipitationLastHour"
    )
    relative_humidity: QuantitativeValue | None = Field(None, alias="relativeHumidity")
    wind_chill: QuantitativeValue | None = Field(None, alias="windChill")
    heat_index: QuantitativeValue | None = Field(None, alias="heatIndex")


class ObservationResponse(BaseModel):
    properties: Observation


class Station(BaseModel):
    id: str
    name: str


def _identity(v: float) -> float:
    return v


# unitCode -> (target unit, converter). Everything is normalized to the same
# imperial units the other collectors use so dashboards can line them up.
CONVERSIONS: dict[str, tuple[str, Callable[[float], float]]] = {
    "wmoUnit:degC": ("f", lambda v: v * 9 / 5 + 32),
    "wmoUnit:degF": ("f", _identity),
    "wmoUnit:km_h-1": ("mph", lambda v: v / 1.609344),
    "wmoUnit:m_s-1": ("mph", lambda v: v * 3600 / 1609.344),
    "wmoUnit:degree_(angle)": ("degree", _identity),
    "wmoUnit:percent": ("pct", _identity),
    "wmoUnit:Pa": ("inhg", lambda v: v / 3386.389),
    "wmoUnit:hPa": ("inhg", lambda v: v / 33.86389),
    "wmoUnit:m": ("mi", lambda v: v / 1609.344),
    "wmoUnit:km": ("mi", lambda v: v / 1.609344),
    "wmoUnit:mm": ("in", lambda v: v / 25.4),
}


class NwsMetricCollector(MetricCollector):
    label_names = [
        "station_id",
        "station_name",
    ]

    default_documentation = "National Weather Service Metric"

    prefix = "nws"

    def __init__(self, session: ClientSession):
        super().__init__(session)
        self.config = NwsConfig()
        self.headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/geo+json",
        }
        self.station: Station | None = None

    async def _get_json(self, url: str) -> Any:
        async with self.session.get(url, headers=self.headers) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def _resolve_station(self) -> Station:
        """
        Resolve and cache the station. Done lazily rather than in initialize()
        so an API outage at startup doesn't take down the whole service.
        """
        if self.station is not None:
            return self.station

        if self.config.station:
            data = await self._get_json(f"{BASE_URL}/stations/{self.config.station}")
            props = data["properties"]
        else:
            # /points only accepts up to 4 decimal places
            point = f"{self.config.latitude:.4f},{self.config.longitude:.4f}"
            points = await self._get_json(f"{BASE_URL}/points/{point}")
            stations = await self._get_json(points["properties"]["observationStations"])
            # Stations are ordered by distance from the point
            props = stations["features"][0]["properties"]

        self.station = Station(id=props["stationIdentifier"], name=props["name"])
        logger.info("Using NWS station %s (%s)", self.station.id, self.station.name)
        return self.station

    def _add(self, name: str, value: QuantitativeValue | None, labels: list[str]):
        # NWS reports null for anything the station didn't measure (e.g. heat index in winter)
        if value is None or value.value is None:
            return

        conversion = CONVERSIONS.get(value.unit_code)
        if conversion is None:
            logger.warning("Unknown NWS unit %s for %s", value.unit_code, name)
            return

        unit, convert = conversion
        self.get_gauge(f"{self.prefix}_{name}", unit).add_metric(
            labels, convert(value.value)
        )

    async def collect_metrics(self):
        station = await self._resolve_station()

        data = await self._get_json(
            f"{BASE_URL}/stations/{station.id}/observations/latest"
        )
        obs = ObservationResponse.model_validate(data).properties

        labels = [station.id, station.name]

        # Stations typically report hourly, so expose the observation time for staleness checks
        self.get_gauge(
            f"{self.prefix}_observation_timestamp",
            "seconds",
            "Time of the latest NWS observation",
        ).add_metric(labels, obs.timestamp.timestamp())

        self._add("temperature", obs.temperature, labels)
        self._add("dewpoint", obs.dewpoint, labels)
        self._add("humidity", obs.relative_humidity, labels)
        self._add("wind_chill", obs.wind_chill, labels)
        self._add("heat_index", obs.heat_index, labels)
        self._add("wind_speed", obs.wind_speed, labels)
        self._add("wind_direction", obs.wind_direction, labels)
        self._add("wind_gust", obs.wind_gust, labels)
        self._add("barometric_pressure", obs.barometric_pressure, labels)
        self._add("sea_level_pressure", obs.sea_level_pressure, labels)
        self._add("visibility", obs.visibility, labels)
        self._add("precipitation_last_hour", obs.precipitation_last_hour, labels)
