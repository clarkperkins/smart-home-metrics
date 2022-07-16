import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from functools import lru_cache

from aiohttp import ClientSession
from prometheus_client import CollectorRegistry, Enum, Gauge

logger = logging.getLogger(__name__)


class MetricCollector(ABC):
    label_names: Sequence[str]

    def __init__(self, registry: CollectorRegistry, session: ClientSession):
        self.registry = registry
        self.session = session

    def _check_labels(self):
        if self.label_names is None:
            raise AssertionError("label_names must be set")

    @lru_cache(None)
    def get_gauge(self, key: str) -> Gauge:
        return Gauge(
            key,
            "SmartThings Device",
            registry=self.registry,
            labelnames=self.label_names,
        )

    @lru_cache(None)
    def get_enum(self, key: str, states: tuple[str]) -> Enum:
        return Enum(
            key,
            "SmartThings Device",
            registry=self.registry,
            labelnames=self.label_names,
            states=states,
        )

    async def initialize(self):
        """
        Perform any initialization logic
        """

    async def perform_collection(self):
        """
        Called to perform metric collection.
        Just a wrapper around the collect_metrics() method to catch any exceptions.
        You shouldn't need to override this method in most cases.
        """
        try:
            await self.collect_metrics()
        except Exception as exc:
            logger.warning("Metric collection failed", exc_info=exc)

    @abstractmethod
    async def collect_metrics(self):
        """
        Perform metric collection. Will be called once per minute.
        """
