import asyncio
import logging

from aiohttp import ClientSession
from prometheus_client import REGISTRY, CollectorRegistry

from shm.collectors import MetricCollector
from shm.collectors.ecobee import EcobeeMetricCollector
from shm.collectors.smartthings import SmartThingsMetricCollector

logger = logging.getLogger(__name__)

ECOBEE_ENABLED = False
ST_ENABLED = True


async def collect_metrics(registry: CollectorRegistry = REGISTRY):
    async with ClientSession() as session:
        collectors: list[MetricCollector] = []

        if ST_ENABLED:
            collectors.append(SmartThingsMetricCollector(registry, session))

        if ECOBEE_ENABLED:
            collectors.append(EcobeeMetricCollector(registry, session))

        # initialize them all
        await asyncio.gather(*[c.initialize() for c in collectors])

        logger.info("Finished initializing collectors")

        while True:
            collect_coros = [c.collect_metrics() for c in collectors]
            await asyncio.gather(*collect_coros)
            await asyncio.sleep(60)
