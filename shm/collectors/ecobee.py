import asyncio
import functools
import logging
import os
from asyncio import Future
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypeVar

from aiohttp import ClientSession
from prometheus_client import CollectorRegistry
from pyecobee import (
    EcobeeAuthorizationException,
    EcobeeService,
    Scope,
    Selection,
    SelectionType,
)

from shm.collectors import MetricCollector

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncEcobeeService(EcobeeService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.executor = ThreadPoolExecutor(
            max_workers=5,
            thread_name_prefix="ecobee",
        )

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
        "sensor_name",
    ]

    def __init__(self, registry: CollectorRegistry, session: ClientSession):
        super().__init__(registry, session)

        client_id = os.environ.get("ECOBEE_CLIENT_ID")
        self.ecobee = AsyncEcobeeService(
            "Thermostat", client_id, scope=Scope.SMART_READ
        )

        self._revisions: dict[str, Revisions] = {}

    async def initialize(self):
        auth_response = await self.ecobee.authorize_async()
        logger.info("Ecobee PIN code: %s", auth_response.ecobee_pin)

    async def _ensure_tokens(self) -> bool:
        if self.ecobee.access_token is None:
            # no token yet, request one
            try:
                await self.ecobee.request_tokens_async()
            except EcobeeAuthorizationException:
                logger.warning("Waiting for code authorization...")
                return False
        else:
            await self.ecobee.refresh_tokens_async()

        return True

    async def collect_metrics(self):
        if not await self._ensure_tokens():
            return

        selection = Selection(
            SelectionType.REGISTERED, "", include_equipment_status=True
        )

        summary = self.ecobee.request_thermostats_summary(selection)

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

            if thermostat_id not in self._revisions:
                pass
