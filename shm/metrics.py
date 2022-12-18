import logging
from collections.abc import Iterable

import anyio
from aiohttp import ClientSession
from prometheus_client import Metric
from prometheus_client.registry import Collector

from shm.collectors import MetricCollector
from shm.collectors.ecobee import EcobeeMetricCollector
from shm.collectors.smartthings import SmartThingsMetricCollector

logger = logging.getLogger(__name__)

ECOBEE_ENABLED = True
ST_ENABLED = True


class SmartHomeCollector(Collector):
    def __init__(self):
        self.collectors: list[MetricCollector] = []
        self.session = ClientSession()

    async def setup_collectors(self):
        logger.info("Initializing collectors")
        if ST_ENABLED:
            self.collectors.append(SmartThingsMetricCollector(self.session))

        if ECOBEE_ENABLED:
            self.collectors.append(EcobeeMetricCollector(self.session))

        # initialize them all
        async with anyio.create_task_group() as group:
            for c in self.collectors:
                group.start_soon(c.initialize)

        logger.info("Finished initializing collectors")

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
