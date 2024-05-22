import base64
import functools
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import anyio
from aiohttp import ClientSession
from kubernetes_asyncio.client import (
    ApiClient,
    ApiException,
    CoreV1Api,
    V1ObjectMeta,
    V1Secret,
)
from kubernetes_asyncio.config import load_incluster_config
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from pyecobee import (
    EcobeeAuthorizationException,
    EcobeeService,
    RemoteSensor,
    RemoteSensorCapability,
    Runtime,
    Scope,
    Selection,
    SelectionType,
    Thermostat,
    Weather,
    WeatherForecast,
)

from shm.collectors import MetricCollector

BASE_DIR = Path(__file__).resolve().parent.parent.parent

ALL_EQUIPMENT = [
    "heatPump",
    "heatPump2",
    "heatPump3",
    "compCool1",
    "compCool2",
    "auxHeat1",
    "auxHeat2",
    "auxHeat3",
    "fan",
    "humidifier",
    "dehumidifier",
    "ventilator",
    "economizer",
    "compHotWater",
    "auxHotWater",
]

logger = logging.getLogger(__name__)

T = TypeVar("T")
UTC = ZoneInfo("UTC")


class EcobeeConfig(BaseSettings):
    client_id: str
    token_store_type: str = "file"
    token_store_file_path: Path = BASE_DIR / "ecobee.json"
    token_store_k8s_namespace: str | None = None
    token_store_k8s_secret_name: str | None = None

    class Config:
        env_prefix = "ECOBEE_"


class EcobeeTokens(BaseModel):
    refresh_token: str | None = None


def to_async(f: Callable[..., T]) -> Callable[..., Coroutine[Any, Any, T]]:
    @functools.wraps(f)
    async def inner(*args, **kwargs) -> T:
        return await anyio.to_thread.run_sync(functools.partial(f, *args, **kwargs))

    return inner


class MetricsEcobeeService(EcobeeService):
    def __init__(self, config: EcobeeConfig = EcobeeConfig()):
        super().__init__("Thermostat", config.client_id, scope=Scope.SMART_READ)
        self.config = config
        self.k8s_api_client: ApiClient | None = None
        if config.token_store_type == "kubernetes":
            load_incluster_config()
            self.k8s_api_client = ApiClient()

    async def load_tokens(self):
        if self.config.token_store_type == "file":
            # try local config first
            @to_async
            def load() -> EcobeeTokens | None:
                if self.config.token_store_file_path.is_file():
                    logger.info(
                        "Loading ecobee config from file: %s",
                        self.config.token_store_file_path,
                    )
                    return EcobeeTokens.parse_file(self.config.token_store_file_path)
                else:
                    return None

            tokens = await load()

            if tokens:
                self.refresh_token = tokens.refresh_token
        elif self.config.token_store_type == "kubernetes":
            core = CoreV1Api(self.k8s_api_client)
            try:
                secret: V1Secret = await core.read_namespaced_secret(
                    self.config.token_store_k8s_secret_name,
                    self.config.token_store_k8s_namespace,
                )
                if secret.data:
                    refresh_token_b64: str = secret.data.get("ecobee_refresh_token")

                    if refresh_token_b64:
                        self.refresh_token = base64.decodebytes(
                            refresh_token_b64.encode("utf8")
                        ).decode("utf8")
                        logger.info(
                            "Loaded refresh token from secret: %s",
                            self.config.token_store_k8s_secret_name,
                        )
            except ApiException as e:
                if e.status == 404:
                    logger.info(
                        "Secret %s not found in namespace %s",
                        self.config.token_store_k8s_secret_name,
                        self.config.token_store_k8s_namespace,
                    )
                else:
                    raise e
        else:
            raise ValueError(
                f"Invalid token store type: {self.config.token_store_type}"
            )

    async def save_tokens(self):
        if self.config.token_store_type == "file":
            tokens = EcobeeTokens(
                refresh_token=self.refresh_token,
            )

            @to_async
            def save(t: EcobeeTokens):
                logger.info(
                    "Saving ecobee config to file: %s",
                    self.config.token_store_file_path,
                )
                with self.config.token_store_file_path.open("wt") as f:
                    f.write(t.json())

            await save(tokens)
        elif self.config.token_store_type == "kubernetes":
            core = CoreV1Api(self.k8s_api_client)

            try:
                secret: V1Secret = await core.read_namespaced_secret(
                    self.config.token_store_k8s_secret_name,
                    self.config.token_store_k8s_namespace,
                )
                if secret.data and "ecobee_refresh_token" in secret.data:
                    del secret.data["ecobee_refresh_token"]
                secret.string_data = {
                    "ecobee_refresh_token": self.refresh_token,
                }
                logger.info(
                    "Updating secret %s with new refresh token",
                    self.config.token_store_k8s_secret_name,
                )
                await core.replace_namespaced_secret(
                    self.config.token_store_k8s_secret_name,
                    self.config.token_store_k8s_namespace,
                    secret,
                )
            except ApiException as e:
                if e.status == 404:
                    secret = V1Secret(
                        metadata=V1ObjectMeta(
                            namespace=self.config.token_store_k8s_namespace,
                            name=self.config.token_store_k8s_secret_name,
                        ),
                        type="Opaque",
                        string_data={
                            "ecobee_refresh_token": self.refresh_token,
                        },
                    )
                    logger.info(
                        "Creating secret %s with new refresh token",
                        self.config.token_store_k8s_secret_name,
                    )
                    await core.create_namespaced_secret(
                        self.config.token_store_k8s_namespace,
                        secret,
                    )
                else:
                    raise e
        else:
            raise ValueError(
                f"Invalid token store type: {self.config.token_store_type}"
            )

    async def initialize_tokens(self):
        await self.load_tokens()

        if self.refresh_token is None:
            auth_response = await self.authorize_async()
            logger.info("Ecobee PIN code: %s", auth_response.ecobee_pin)

    async def ensure_tokens(self) -> bool:
        now_utc_plus_30 = datetime.now(UTC) + timedelta(seconds=30)

        if self.access_token is None and self.refresh_token is None:
            # no tokens yet, request one
            try:
                await self.request_tokens_async()
                await self.save_tokens()
            except EcobeeAuthorizationException:
                logger.warning("Waiting for code authorization...")
                return False
        elif (self.access_token is None) or (
            self.access_token_expires_on < now_utc_plus_30
        ):
            await self.refresh_tokens_async()
            await self.save_tokens()

        return True

    def __getattr__(self, item: str):
        async_suffix = "_async"

        if item.endswith(async_suffix):
            method_name = item.removesuffix(async_suffix)
            method = getattr(self, method_name)
            return to_async(method)
        else:
            raise AttributeError()


@dataclass()
class Revisions:
    thermostat: str
    alerts: str
    runtime: str
    interval: str


class EcobeeMetricCollector(MetricCollector):
    label_names = [
        "thermostat_id",
        "thermostat_name",
        "sensor_name",
    ]
    default_documentation = "Ecobee Device"

    def __init__(self, session: ClientSession):
        super().__init__(session)
        self.ecobee = MetricsEcobeeService()

        self._revisions: dict[str, Revisions] = {}
        self._cache: dict[str, Thermostat] = {}

    async def initialize(self):
        await self.ecobee.initialize_tokens()

    async def collect_metrics(self):
        if not await self.ecobee.ensure_tokens():
            return

        actual_temp = self.get_gauge("ecobee_actual_temperature", unit="f")
        raw_temp = self.get_gauge("ecobee_raw_temperature", unit="f")
        actual_humid = self.get_gauge("ecobee_actual_humidity", unit="pct")
        desired_heat = self.get_gauge("ecobee_desired_heat", unit="f")
        desired_cool = self.get_gauge("ecobee_desired_cool", unit="f")
        actual_voc = self.get_gauge("ecobee_actual_voc", unit="ppb")
        actual_co2 = self.get_gauge("ecobee_actual_co2", unit="ppm")
        outdoor_temp = self.get_gauge("ecobee_outdoor_temperature", unit="f")

        equipment_status = self.get_enum("ecobee_equipment_status")

        sensor_temp = self.get_gauge("ecobee_sensor_temperature", unit="f")
        sensor_occupancy = self.get_gauge("ecobee_sensor_occupancy")
        sensor_humidity = self.get_gauge("ecobee_sensor_humidity", unit="pct")

        summary = await self.ecobee.request_thermostats_summary_async(
            Selection(
                SelectionType.REGISTERED.value,
                "",
            ),
            timeout=15,
        )

        new_thermostat_ids: set[str] = set()
        thermostats: list[Thermostat] = []

        async def _get_thermostat(s: Selection):
            r = await self.ecobee.request_thermostats_async(s, timeout=15)
            thermostats.append(r.thermostat_list[0])

        async with anyio.create_task_group() as group:
            for revision in summary.revision_list:
                (
                    thermostat_id,
                    thermostat_name,
                    connected,
                    thermostat_revision,
                    alerts_revision,
                    runtime_revision,
                    interval_revision,
                ) = revision.split(":")

                new_thermostat_ids.add(thermostat_id)

                new_revisions = Revisions(
                    thermostat_revision,
                    alerts_revision,
                    runtime_revision,
                    interval_revision,
                )

                old_revisions = self._revisions.get(thermostat_id)

                selection = Selection(
                    SelectionType.THERMOSTATS.value,
                    thermostat_id,
                    include_equipment_status=True,
                    include_weather=True,
                )

                if old_revisions:
                    if old_revisions.thermostat != new_revisions.thermostat:
                        logger.debug(
                            "Thermostat revision changed for %s, requesting new settings and device data",
                            thermostat_name,
                        )
                        selection.include_settings = True
                        selection.include_program = True
                        selection.include_events = True
                        selection.include_device = True

                    if old_revisions.alerts != new_revisions.alerts:
                        logger.debug(
                            "Alerts revision changed for %s, requesting new alert data",
                            thermostat_name,
                        )
                        selection.include_alerts = True

                    if old_revisions.runtime != new_revisions.runtime:
                        logger.debug(
                            "Runtime revision changed for %s, requesting new remote sensor data",
                            thermostat_name,
                        )
                        selection.include_runtime = True
                        selection.include_sensors = True

                    if old_revisions.interval != new_revisions.interval:
                        logger.debug(
                            "Interval revision changed for %s, requesting new runtime data",
                            thermostat_name,
                        )
                        selection.include_extended_runtime = True
                else:
                    # just get everything
                    selection.include_settings = True
                    selection.include_program = True
                    selection.include_events = True
                    selection.include_device = True
                    selection.include_alerts = True
                    selection.include_sensors = True
                    selection.include_runtime = True
                    selection.include_extended_runtime = True

                group.start_soon(_get_thermostat, selection)
                self._revisions[thermostat_id] = new_revisions

        for thermostat in thermostats:
            old_thermostat = self._cache.get(thermostat.identifier)

            # Copy things over from the previous if we don't have new data
            if old_thermostat is not None:
                if not thermostat.runtime:
                    thermostat._runtime = old_thermostat.runtime
                if not thermostat.remote_sensors:
                    thermostat._remote_sensors = old_thermostat.remote_sensors

            self._cache[thermostat.identifier] = thermostat

        # Delete any thermostats from the cache that no longer exist
        thermostat_ids_to_remove = self._cache.keys() - new_thermostat_ids
        for thermostat_id in thermostat_ids_to_remove:
            del self._cache[thermostat_id]

        for thermostat in self._cache.values():
            labels = [
                thermostat.identifier,
                thermostat.name,
                "",
            ]

            running_equipment = set(thermostat.equipment_status.split(","))
            runtime: Runtime = thermostat.runtime

            equipment_status.add_metric(
                labels, {e: e in running_equipment for e in ALL_EQUIPMENT}
            )

            actual_temp.add_metric(labels, runtime.actual_temperature / 10)
            raw_temp.add_metric(labels, runtime.raw_temperature / 10)
            actual_humid.add_metric(labels, runtime.actual_humidity)
            desired_heat.add_metric(labels, runtime.desired_heat / 10)
            desired_cool.add_metric(labels, runtime.desired_cool / 10)
            actual_voc.add_metric(labels, runtime.actual_voc)
            actual_co2.add_metric(labels, runtime.actual_co2)

            weather: Weather = thermostat.weather
            forecast: WeatherForecast = weather.forecasts[0]
            outdoor_temp.add_metric(labels, forecast.temperature / 10)

            remote_sensors: list[RemoteSensor] = thermostat.remote_sensors

            for sensor in remote_sensors:
                sensor_labels = [
                    thermostat.identifier,
                    thermostat.name,
                    sensor.name,
                ]
                capabilities: list[RemoteSensorCapability] = sensor.capability
                for capability in capabilities:
                    if capability.type == "temperature":
                        sensor_temp.add_metric(
                            sensor_labels, int(capability.value) / 10
                        )
                    elif capability.type == "occupancy":
                        sensor_occupancy.add_metric(
                            sensor_labels, 1 if capability.value == "true" else 0
                        )
                    elif capability.type == "humidity":
                        sensor_humidity.add_metric(sensor_labels, int(capability.value))
