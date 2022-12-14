import asyncio
import logging
from collections.abc import Iterable

from aiohttp import ClientSession
from prometheus_client import Metric
from prometheus_client.registry import Collector

from shm.collectors import MetricCollector
from shm.collectors.ecobee import EcobeeMetricCollector
from shm.collectors.smartthings import SmartThingsMetricCollector

logger = logging.getLogger(__name__)

ECOBEE_ENABLED = False
ST_ENABLED = True


class SmartHomeCollector(Collector):
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.collectors: list[MetricCollector] = []
        self.session: ClientSession | None = None
        self.loop.run_until_complete(self.setup_collectors())

    async def setup_collectors(self):
        self.session = ClientSession(loop=self.loop)
        if ST_ENABLED:
            self.collectors.append(SmartThingsMetricCollector(self.session))

        if ECOBEE_ENABLED:
            self.collectors.append(EcobeeMetricCollector(self.session))

        # initialize them all
        await asyncio.gather(*[c.initialize() for c in self.collectors])
        logger.info("Finished initializing collectors")

    def collect(self) -> Iterable[Metric]:
        return self.loop.run_until_complete(self.do_collect())

    async def do_collect(self) -> Iterable[Metric]:
        collect_coros = [c.perform_collection() for c in self.collectors]
        all_metric_iterables = await asyncio.gather(*collect_coros)

        all_metrics: list[Metric] = []

        for metric_iterable in all_metric_iterables:
            all_metrics.extend(metric_iterable)

        return all_metrics
