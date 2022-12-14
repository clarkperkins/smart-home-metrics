import asyncio
import functools
import logging
import os
from asyncio import Future
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TypeVar
from zoneinfo import ZoneInfo

from aiohttp import ClientSession
from prometheus_client import Metric
from pydantic import BaseModel
from pyecobee import (
    EcobeeAuthorizationException,
    EcobeeService,
    EcobeeThermostatResponse,
    RemoteSensor,
    RemoteSensorCapability,
    Runtime,
    Scope,
    Selection,
    SelectionType,
    Thermostat,
)

from shm.collectors import MetricCollector

BASE_DIR = Path(__file__).resolve().parent.parent.parent

CONFIG_FILE = BASE_DIR / "ecobee.json"

logger = logging.getLogger(__name__)

T = TypeVar("T")
UTC = ZoneInfo("UTC")


class EcobeeTokens(BaseModel):
    access_token: Optional[str] = None
    access_token_expires_on: Optional[datetime] = None
    refresh_token: Optional[str] = None
    refresh_token_expires_on: Optional[datetime] = None


class MetricsEcobeeService(EcobeeService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.executor = ThreadPoolExecutor(
            max_workers=5,
            thread_name_prefix="ecobee",
        )

    @classmethod
    def build(cls, client_id: str):
        return cls(
            "Thermostat",
            client_id,
            scope=Scope.SMART_READ,
        )

    async def load_tokens(self):
        # try local config first
        def load() -> Optional[EcobeeTokens]:
            if CONFIG_FILE.is_file():
                return EcobeeTokens.parse_file(CONFIG_FILE)
            else:
                return None

        config = await self.to_async(load)()

        if config:
            self.access_token = config.access_token
            self.access_token_expires_on = config.access_token_expires_on
            self.refresh_token = config.refresh_token
            self.refresh_token_expires_on = config.refresh_token_expires_on

    async def save_tokens(self):
        config = EcobeeTokens(
            access_token=self.access_token,
            access_token_expires_on=self.access_token_expires_on,
            refresh_token=self.refresh_token,
            refresh_token_expires_on=self.refresh_token_expires_on,
        )

        def save(tokens: EcobeeTokens):
            with CONFIG_FILE.open("wt") as f:
                f.write(tokens.json())

        await self.to_async(save)(config)

    async def initialize_tokens(self):
        await self.load_tokens()

        if self.access_token is None:
            auth_response = await self.authorize_async()
            logger.info("Ecobee PIN code: %s", auth_response.ecobee_pin)

    async def ensure_tokens(self) -> bool:
        now_utc_plus_30 = datetime.now(UTC) + timedelta(seconds=30)

        if self.access_token is None:
            # no token yet, request one
            try:
                await self.request_tokens_async()
                await self.save_tokens()
            except EcobeeAuthorizationException:
                logger.warning("Waiting for code authorization...")
                return False
        elif self.access_token_expires_on < now_utc_plus_30:
            await self.refresh_tokens_async()
            await self.save_tokens()

        return True

    def to_async(self, f: Callable[..., T]) -> Callable[..., Future[T]]:
        @functools.wraps(f)
        def inner(*args, **kwargs) -> Future[T]:
            loop = asyncio.get_running_loop()
            return loop.run_in_executor(
                self.executor, functools.partial(f, *args, **kwargs)
            )

        return inner

    def __getattr__(self, item: str):
        async_suffix = "_async"

        if item.endswith(async_suffix):
            method_name = item.removesuffix(async_suffix)
            method = getattr(self, method_name)
            return self.to_async(method)
        else:
            raise AttributeError()


@dataclass()
class Revisions:
    thermostat: str
    alerts: str
    runtime: str
    internal: str


class EcobeeMetricCollector(MetricCollector):
    label_names = [
        "thermostat_id",
        "thermostat_name",
        "sensor_name",
        # "device_id",
        # "device_name",
        # "device_label",
        # "location_id",
        # "location_name",
        # "room_id",
        # "room_name",
        # "type",
        # "device_type_id",
        # "device_type_name",
        # "device_type_network",
    ]
    default_documentation = "Ecobee Device"

    def __init__(self, session: ClientSession):
        super().__init__(session)

        client_id = os.environ.get("ECOBEE_CLIENT_ID")
        if client_id is None:
            raise EnvironmentError("Missing ECOBEE_CLIENT_ID env var")
        self.ecobee = MetricsEcobeeService.build(client_id)

        self._revisions: dict[str, Revisions] = {}
        self._cache: dict[str, Thermostat] = {}

    async def initialize(self):
        await self.ecobee.initialize_tokens()

    async def collect_metrics(self) -> Iterable[Metric]:
        if not await self.ecobee.ensure_tokens():
            return []

        actual_temp = self.get_gauge("ecobee_actual_temperature", unit="f")
        raw_temp = self.get_gauge("ecobee_raw_temperature", unit="f")
        actual_humid = self.get_gauge("ecobee_actual_humidity", unit="pct")
        desired_heat = self.get_gauge("ecobee_desired_heat", unit="f")
        desired_cool = self.get_gauge("ecobee_desired_cool", unit="f")
        actual_voc = self.get_gauge("ecobee_actual_voc", unit="ppb")
        actual_co2 = self.get_gauge("ecobee_actual_co2", unit="ppm")

        sensor_temp = self.get_gauge("ecobee_sensor_temperature", unit="f")
        sensor_occupancy = self.get_gauge("ecobee_sensor_occupancy")
        sensor_humidity = self.get_gauge("ecobee_sensor_humidity", unit="pct")

        selection = Selection(
            SelectionType.REGISTERED.value,
            "",
            include_equipment_status=True,
        )

        summary = await self.ecobee.request_thermostats_summary_async(selection)

        thermostat_requests = []

        for revision in summary.revision_list:
            (
                thermostat_id,
                thermostat_name,
                connected,
                thermostat_revision,
                alerts_revision,
                runtime_revision,
                internal_revision,
            ) = revision.split(":")

            new_revisions = Revisions(
                thermostat_revision,
                alerts_revision,
                runtime_revision,
                internal_revision,
            )

            old_revisions = self._revisions.get(thermostat_id)

            selection = Selection(
                SelectionType.THERMOSTATS.value,
                thermostat_id,
                include_equipment_status=True,
            )

            should_request = False

            # if old_revisions is None or old_revisions.thermostat != new_revisions.thermostat:
            #     should_request = True
            #     selection.include_settings = True
            #     selection.include_program = True
            #     selection.include_events = True
            #     selection.include_device = True

            # if old_revisions is None or old_revisions.alerts != new_revisions.alerts:
            #     should_request = True
            #     selection.include_alerts = True

            if old_revisions is None or old_revisions.runtime != new_revisions.runtime:
                should_request = True
                selection.include_sensors = True

            if (
                old_revisions is None
                or old_revisions.internal != new_revisions.internal
            ):
                should_request = True
                selection.include_runtime = True

            if should_request:
                thermostat_requests.append(
                    self.ecobee.request_thermostats_async(selection)
                )

            self._revisions[thermostat_id] = new_revisions

        if thermostat_requests:
            thermostats: Sequence[EcobeeThermostatResponse] = await asyncio.gather(
                *thermostat_requests
            )

            for r in thermostats:
                thermostat: Thermostat = r.thermostat_list[0]

                old_thermostat = self._cache.get(thermostat.identifier)

                # Copy things over from the previous if we don't have new data
                if old_thermostat is not None:
                    if not thermostat.runtime:
                        thermostat._runtime = old_thermostat.runtime
                    if not thermostat.remote_sensors:
                        thermostat._remote_sensors = old_thermostat.remote_sensors

                self._cache[thermostat.identifier] = thermostat

        for thermostat in self._cache.values():
            runtime: Runtime = thermostat.runtime

            labels = [
                thermostat.identifier,
                thermostat.name,
            ]

            actual_temp.add_metric(labels, runtime.actual_temperature / 10)
            raw_temp.add_metric(labels, runtime.raw_temperature / 10)
            actual_humid.add_metric(labels, runtime.actual_humidity)
            desired_heat.add_metric(labels, runtime.desired_heat / 10)
            desired_cool.add_metric(labels, runtime.desired_cool / 10)
            actual_voc.add_metric(labels, runtime.actual_voc)
            actual_co2.add_metric(labels, runtime.actual_co2)

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

        metrics = [
            actual_temp,
            raw_temp,
            actual_humid,
            desired_heat,
            desired_cool,
            actual_voc,
            actual_co2,
            sensor_temp,
            sensor_occupancy,
            sensor_humidity,
        ]

        return metrics
