import logging

from aiohttp import ClientSession
from pydantic import BaseModel, BaseSettings

from shm.collectors import MetricCollector

logger = logging.getLogger(__name__)


class WeatherApiConfig(BaseSettings):
    key: str
    query: str

    class Config:
        env_prefix = "WEATHERAPI_"


class Location(BaseModel):
    name: str
    region: str


class Current(BaseModel):
    temp_f: float
    feelslike_f: float
    humidity: float
    wind_mph: float
    wind_degree: float
    gust_mph: float


class WeatherData(BaseModel):
    location: Location
    current: Current


class WeatherApiMetricCollector(MetricCollector):
    label_names = [
        "location_name",
        "location_region",
    ]

    default_documentation = "Weather API Metric"

    prefix = "weatherapi"

    def __init__(self, session: ClientSession):
        super().__init__(session)
        self.config = WeatherApiConfig()

    async def collect_metrics(self):
        temp = self.get_gauge(f"{self.prefix}_temperature", "f")
        feels_like_temp = self.get_gauge(f"{self.prefix}_feels_like_temperature", "f")
        humidity = self.get_gauge(f"{self.prefix}_humidity", "pct")
        wind_speed = self.get_gauge(f"{self.prefix}_wind_speed", "mph")
        wind_direction = self.get_gauge(f"{self.prefix}_wind_direction", "degree")
        wind_gust = self.get_gauge(f"{self.prefix}_wind_gust", "mph")

        current_url = f"https://api.weatherapi.com/v1/current.json?key={self.config.key}&q={self.config.query}"

        async with self.session.get(current_url) as r:
            data = WeatherData.parse_obj(await r.json())

            labels = [
                data.location.name,
                data.location.region,
            ]

            temp.add_metric(labels, data.current.temp_f)
            feels_like_temp.add_metric(labels, data.current.feelslike_f)
            humidity.add_metric(labels, data.current.humidity)
            wind_speed.add_metric(labels, data.current.wind_mph)
            wind_direction.add_metric(labels, data.current.wind_degree)
            wind_gust.add_metric(labels, data.current.gust_mph)
