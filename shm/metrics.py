import asyncio
import logging
import threading

from aiohttp import ClientSession
from prometheus_client import REGISTRY, CollectorRegistry, make_asgi_app
from uvicorn import Config, Server

from shm.collectors import MetricCollector
from shm.collectors.ecobee import EcobeeMetricCollector
from shm.collectors.smartthings import SmartThingsMetricCollector

logger = logging.getLogger(__name__)

ECOBEE_ENABLED = False
ST_ENABLED = True


class Collector:
    def __init__(self, host: str, port: int, registry: CollectorRegistry = REGISTRY):
        self.registry = registry
        self.server = self.make_prometheus_server(host, port)

    @staticmethod
    def make_prometheus_server(host: str, port: int):
        app = make_asgi_app()
        config = Config(app, host=host, port=port, log_config=None)
        return Server(config=config)

    async def main_loop(self):
        async with ClientSession() as session:
            collectors: list[MetricCollector] = []

            if ST_ENABLED:
                collectors.append(SmartThingsMetricCollector(self.registry, session))

            if ECOBEE_ENABLED:
                collectors.append(EcobeeMetricCollector(self.registry, session))

            # initialize them all
            await asyncio.gather(*[c.initialize() for c in collectors])

            logger.info("Finished initializing collectors")

            counter = 0

            # rely on the server signal handler to set should_exit
            while not self.server.should_exit:
                if counter == 0:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Active threads: %s",
                            ", ".join(t.name for t in threading.enumerate()),
                        )
                    collect_coros = [c.perform_collection() for c in collectors]
                    await asyncio.gather(*collect_coros)

                counter += 1
                counter %= 60
                await asyncio.sleep(1)

            logger.info("Metrics collection shut down")

    async def run(self):
        await asyncio.gather(
            self.server.serve(),
            self.main_loop(),
        )
