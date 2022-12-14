import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence

from aiohttp import ClientSession
from prometheus_client import Metric
from prometheus_client.core import GaugeMetricFamily, StateSetMetricFamily

logger = logging.getLogger(__name__)


class MetricCollector(ABC):
    label_names: Sequence[str]
    default_documentation: str

    def __init__(self, session: ClientSession):
        self.session = session

    def _check_labels(self):
        if self.label_names is None:
            raise AssertionError("label_names must be set")

    def get_gauge(
        self, key: str, unit: str = "", documentation: str | None = None
    ) -> GaugeMetricFamily:
        self._check_labels()
        return GaugeMetricFamily(
            key,
            documentation or self.default_documentation,
            labels=self.label_names,
            unit=unit,
        )

    def get_enum(
        self, key: str, documentation: str | None = None
    ) -> StateSetMetricFamily:
        self._check_labels()
        return StateSetMetricFamily(
            key,
            documentation or self.default_documentation,
            labels=self.label_names,
        )

    async def initialize(self):
        """
        Perform any initialization logic
        """

    async def perform_collection(self) -> Iterable[Metric]:
        """
        Called to perform metric collection.
        Just a wrapper around the collect_metrics() method to catch any exceptions.
        You shouldn't need to override this method in most cases.
        """
        try:
            return await self.collect_metrics()
        except Exception as exc:
            logger.warning("Metric collection failed", exc_info=exc)
            return []

    @abstractmethod
    async def collect_metrics(self) -> Iterable[Metric]:
        """
        Perform metric collection. Will be called once per minute.
        """
