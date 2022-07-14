from abc import ABC, abstractmethod
from collections.abc import Sequence
from functools import lru_cache

from aiohttp import ClientSession
from prometheus_client import CollectorRegistry, Enum, Gauge


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

    @abstractmethod
    async def collect_metrics(self):
        """
        Perform metric collection. Will be called once per minute.
        """
