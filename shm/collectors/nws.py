import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Self

import anyio
from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from shm.collectors import MetricCollector

logger = logging.getLogger(__name__)

BASE_URL = "https://api.weather.gov"

DEFAULT_USER_AGENT = "smart-home-metrics (github.com/clarkperkins/smart-home-metrics)"

# How long to wait before retrying a station/point the API said doesn't exist
INVALID_RETRY_SECONDS = 60 * 60
# How often to re-resolve the nearest stations for a point, so a retired or
# long-dead station is eventually replaced by the next closest one
POINT_TTL_SECONDS = 24 * 60 * 60


class NwsConfig(BaseSettings):
    """
    Stations can be listed explicitly with `stations` (e.g. NWS_STATIONS=KAUS,KATT)
    and/or found from `latitude` + `longitude`, which adds the `nearest_stations`
    closest observation stations from the /points API. The collector is enabled
    when either is set.
    """

    model_config = SettingsConfigDict(env_prefix="NWS_")

    stations: Annotated[list[str], NoDecode] = []
    latitude: float | None = None
    longitude: float | None = None
    nearest_stations: int = Field(1, ge=1)
    # Per-request timeout in seconds. Resolving a point costs 2 sequential requests
    # before the observation fetch, so the worst case scrape is 3x this, which must
    # stay under the ServiceMonitor's 30s scrape timeout.
    timeout: float = Field(5, gt=0, le=9)
    # api.weather.gov requires a User-Agent identifying the application,
    # ideally with contact info: https://www.weather.gov/documentation/services-web-api
    user_agent: str = DEFAULT_USER_AGENT

    @field_validator("stations", mode="before")
    @classmethod
    def split_stations(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.split(",")
        if isinstance(v, list):
            # Normalize and de-duplicate, keeping the configured order
            return list(dict.fromkeys(s.strip().upper() for s in v if s.strip()))
        return v

    @property
    def has_point(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def enabled(self) -> bool:
        return bool(self.stations) or self.has_point

    @model_validator(mode="after")
    def check_point(self) -> Self:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("NWS_LATITUDE and NWS_LONGITUDE must be set together")
        return self


class QuantitativeValue(BaseModel):
    unit_code: str = Field(alias="unitCode")
    value: float | None = None
    quality_control: str | None = Field(None, alias="qualityControl")


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


# Source unit (without its "wmoUnit:"/"unit:" prefix) -> (dimension, to SI base unit)
SOURCE_UNITS: dict[str, tuple[str, Callable[[float], float]]] = {
    "degC": ("temperature", _identity),
    "degF": ("temperature", lambda v: (v - 32) * 5 / 9),
    "K": ("temperature", lambda v: v - 273.15),
    "m_s-1": ("speed", _identity),
    "km_h-1": ("speed", lambda v: v / 3.6),
    "Pa": ("pressure", _identity),
    "hPa": ("pressure", lambda v: v * 100),
    "m": ("length", _identity),
    "km": ("length", lambda v: v * 1000),
    "cm": ("length", lambda v: v / 100),
    "mm": ("length", lambda v: v / 1000),
    "degree_(angle)": ("angle", _identity),
    "percent": ("percent", _identity),
}

# Target unit -> (dimension, from SI base unit). Everything is exported in the same
# imperial units the other collectors use so dashboards can line them up.
TARGET_UNITS: dict[str, tuple[str, Callable[[float], float]]] = {
    "f": ("temperature", lambda v: v * 9 / 5 + 32),
    "mph": ("speed", lambda v: v * 3600 / 1609.344),
    "inhg": ("pressure", lambda v: v / 3386.389),
    "mi": ("length", lambda v: v / 1609.344),
    "in": ("length", lambda v: v / 0.0254),
    "degree": ("angle", _identity),
    "pct": ("percent", _identity),
}

# (metric name, observation field, target unit). The target unit is fixed per field
# so the series name doesn't change if NWS changes the unit it reports in.
FIELDS: list[tuple[str, str, str]] = [
    ("temperature", "temperature", "f"),
    ("dewpoint", "dewpoint", "f"),
    ("humidity", "relative_humidity", "pct"),
    ("wind_chill", "wind_chill", "f"),
    ("heat_index", "heat_index", "f"),
    ("wind_speed", "wind_speed", "mph"),
    ("wind_direction", "wind_direction", "degree"),
    ("wind_gust", "wind_gust", "mph"),
    ("barometric_pressure", "barometric_pressure", "inhg"),
    ("sea_level_pressure", "sea_level_pressure", "inhg"),
    ("visibility", "visibility", "mi"),
    ("precipitation_last_hour", "precipitation_last_hour", "in"),
]

# MADIS quality control flag for values that failed QC
QC_REJECTED = "X"


def _convert(value: float, unit_code: str, target: str) -> float | None:
    source = SOURCE_UNITS.get(unit_code.rsplit(":", 1)[-1])
    target_dimension, from_base = TARGET_UNITS[target]
    if source is None or source[0] != target_dimension:
        return None
    return from_base(source[1](value))


def _is_invalid(exc: Exception) -> bool:
    """
    The API says the station/point doesn't exist (e.g. a typo in NWS_STATIONS).
    Other 4xx (403 from the CDN blocking a user agent, 408, 429) are transient.
    """
    return isinstance(exc, ClientResponseError) and exc.status in (400, 404)


def _describe(exc: Exception) -> str:
    # ClientResponseError's str() includes the request URL, and the /points and
    # /gridpoints URLs encode the configured location
    if isinstance(exc, ClientResponseError):
        return f"HTTP {exc.status} {exc.message}"
    return str(exc) or type(exc).__name__


def _log_failure(action: str, exc: Exception):
    # Expected HTTP/network failures get a one-liner; anything else keeps the traceback
    if isinstance(exc, (ClientError, TimeoutError)):
        logger.warning("Failed to %s: %s", action, _describe(exc))
    else:
        logger.warning("Failed to %s", action, exc_info=exc)


def _station(props: dict[str, Any]) -> Station:
    return Station(id=props["stationIdentifier"], name=props["name"])


class NwsMetricCollector(MetricCollector):
    label_names = [
        "station_id",
        "station_name",
    ]

    default_documentation = "National Weather Service Metric"

    prefix = "nws"

    def __init__(self, session: ClientSession, config: NwsConfig | None = None):
        super().__init__(session)
        self.config = config or NwsConfig()
        if not self.config.enabled:
            raise ValueError(
                "NWS_STATIONS or NWS_LATITUDE and NWS_LONGITUDE must be set"
            )
        if self.config.user_agent == DEFAULT_USER_AGENT:
            logger.warning(
                "NWS_USER_AGENT is not set; NWS asks for a User-Agent with contact "
                "info and may block the shared default"
            )
        self.timeout = ClientTimeout(total=self.config.timeout)
        self.headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/geo+json",
        }
        # Resolved lazily and cached rather than in initialize(), so an API outage
        # at startup doesn't take down the whole service. Failures are retried on
        # the next scrape, or after INVALID_RETRY_SECONDS if the API says they don't
        # exist. Keyed by the configured ID, which may differ from stationIdentifier.
        self.stations: dict[str, Station] = {}
        self.invalid_until: dict[str, float] = {}
        self.point_stations: list[Station] = []
        self.point_expires = 0.0

    async def _get_json(self, url: str) -> Any:
        async with self.session.get(
            url, headers=self.headers, timeout=self.timeout
        ) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def _resolve_station(self, station_id: str):
        try:
            data = await self._get_json(f"{BASE_URL}/stations/{station_id}")
            station = _station(data["properties"])
        except Exception as exc:
            if _is_invalid(exc):
                logger.error(
                    "Ignoring NWS station %s for %ss: %s",
                    station_id,
                    INVALID_RETRY_SECONDS,
                    _describe(exc),
                )
                self.invalid_until[station_id] = (
                    time.monotonic() + INVALID_RETRY_SECONDS
                )
            else:
                _log_failure(f"resolve NWS station {station_id}", exc)
            return

        logger.info("Using NWS station %s (%s)", station.id, station.name)
        self.stations[station_id] = station

    async def _resolve_point(self):
        # The configured location is deliberately kept out of every log message here.
        # /points only accepts up to 4 decimal places
        point = f"{self.config.latitude:.4f},{self.config.longitude:.4f}"
        try:
            points = await self._get_json(f"{BASE_URL}/points/{point}")
            stations_url = points["properties"]["observationStations"]
        except Exception as exc:
            if _is_invalid(exc):
                # e.g. a point outside NWS coverage
                logger.error(
                    "Ignoring the configured NWS point for %ss: %s",
                    INVALID_RETRY_SECONDS,
                    _describe(exc),
                )
                self.point_expires = time.monotonic() + INVALID_RETRY_SECONDS
            else:
                _log_failure("resolve the configured NWS point", exc)
            return

        # The point itself is valid, so any failure past here is retried next scrape
        try:
            # Stations are ordered by distance from the point
            stations = await self._get_json(
                f"{stations_url}?limit={self.config.nearest_stations}"
            )
            features = stations["features"][: self.config.nearest_stations]
            point_stations = [_station(f["properties"]) for f in features]
        except Exception as exc:
            # Keep using any previously resolved stations until this succeeds
            _log_failure("resolve NWS stations for the configured point", exc)
            return

        for station in point_stations:
            logger.info("Using NWS station %s (%s)", station.id, station.name)
        self.point_stations = point_stations
        self.point_expires = time.monotonic() + POINT_TTL_SECONDS

    async def _resolve_stations(self):
        now = time.monotonic()
        async with anyio.create_task_group() as group:
            for station_id in self.config.stations:
                if (
                    station_id not in self.stations
                    and self.invalid_until.get(station_id, 0) <= now
                ):
                    group.start_soon(self._resolve_station, station_id)
            if self.config.has_point and self.point_expires <= now:
                group.start_soon(self._resolve_point)

    def _add(
        self, name: str, value: QuantitativeValue | None, unit: str, labels: list[str]
    ):
        # NWS reports null for anything the station didn't measure (e.g. heat index in winter)
        if value is None or value.value is None:
            return

        # Values that failed QC are dropped; questionable ones (Q/B) are still exported
        if value.quality_control == QC_REJECTED:
            return

        converted = _convert(value.value, value.unit_code, unit)
        if converted is None:
            logger.warning("Unexpected NWS unit %s for %s", value.unit_code, name)
            return

        self.get_gauge(f"{self.prefix}_{name}", unit).add_metric(labels, converted)

    async def collect_metrics(self):
        await self._resolve_stations()

        # De-duplicate stations that were both configured and found from the point
        stations = {s.id: s for s in [*self.stations.values(), *self.point_stations]}

        async with anyio.create_task_group() as group:
            for station in stations.values():
                group.start_soon(self._collect_station, station)

    async def _collect_station(self, station: Station):
        # Isolate failures so one flaky station doesn't drop the others
        try:
            # Without require_qc the API strips the latest observation for
            # mesonet (non-METAR) stations down to just the wind gust
            data = await self._get_json(
                f"{BASE_URL}/stations/{station.id}/observations/latest?require_qc=true"
            )
            obs = ObservationResponse.model_validate(data).properties
        except Exception as exc:
            _log_failure(f"fetch NWS observation for {station.id}", exc)
            return

        labels = [station.id, station.name]

        # Stations typically report hourly, so expose the observation time for staleness checks
        self.get_gauge(
            f"{self.prefix}_observation_timestamp",
            "seconds",
            "Time of the latest NWS observation",
        ).add_metric(labels, obs.timestamp.timestamp())

        for name, field, unit in FIELDS:
            self._add(name, getattr(obs, field), unit, labels)
