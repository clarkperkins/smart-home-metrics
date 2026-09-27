import logging
from collections.abc import Iterable

import anyio
import anyio.from_thread
from aiohttp import ClientSession, ClientTimeout
from prometheus_client import Metric
from prometheus_client.registry import Collector
from pydantic import ValidationError

from shm.collectors import MetricCollector
from shm.collectors.ecobee import EcobeeMetricCollector
from shm.collectors.nws import NwsConfig, NwsMetricCollector
from shm.collectors.smartthings import SmartThingsMetricCollector
from shm.collectors.weatherapi import WeatherApiMetricCollector

logger = logging.getLogger(__name__)

ECOBEE_ENABLED = True
ST_ENABLED = True
WEATHERAPI_ENABLED = True
NWS_ENABLED = True

# Default per-request timeout for collectors using the shared session. Without it
# aiohttp allows 300s, and one hung request stalls the whole scrape past the
# ServiceMonitor's 30s scrape timeout, dropping every collector's metrics.
REQUEST_TIMEOUT = ClientTimeout(total=10)


class SmartHomeCollector(Collector):
    def __init__(self):
        self.collectors: list[MetricCollector] = []
        self.session = ClientSession(timeout=REQUEST_TIMEOUT)

    async def setup_collectors(self):
        logger.info("Initializing collectors")
        if ST_ENABLED:
            self.collectors.append(SmartThingsMetricCollector(self.session))

        if ECOBEE_ENABLED:
            self.collectors.append(EcobeeMetricCollector(self.session))

        if WEATHERAPI_ENABLED:
            self.collectors.append(WeatherApiMetricCollector(self.session))

        if NWS_ENABLED:
            self._setup_nws()

        # initialize them all
        async with anyio.create_task_group() as group:
            for c in self.collectors:
                group.start_soon(c.initialize)

        logger.info("Finished initializing collectors")

    def _setup_nws(self):
        # NWS is opt-in, and a bad NWS config skips just that collector rather than
        # failing startup and taking the other collectors down with it
        try:
            config = NwsConfig()
        except ValidationError as exc:
            # Only the field names and messages: the input values include the location
            errors = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'config'}: {e['msg']}"
                for e in exc.errors()
            )
            logger.error("Skipping NWS collector, invalid configuration: %s", errors)
            return

        if config.enabled:
            self.collectors.append(NwsMetricCollector(self.session, config))
        else:
            logger.info("Skipping NWS collector, NWS_STATIONS/NWS_LATITUDE not set")

    def describe(self) -> Iterable[Metric]:
        """
        Don't particularly care about name clashing, just do them all here
        :return:
        """
        return []

    def collect(self) -> Iterable[Metric]:
        return anyio.from_thread.run(self.do_collect)

    async def do_collect(self) -> Iterable[Metric]:
        all_metrics: list[Metric] = []

        async def _collect(c: MetricCollector):
            all_metrics.extend(await c.perform_collection())

        async with anyio.create_task_group() as group:
            for collector in self.collectors:
                group.start_soon(_collect, collector)

        return all_metrics
