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
        self._check_labels()
        self.session = session
        self.metrics: dict[str, Metric] = {}

    def _check_labels(self):
        if self.label_names is None:
            raise AssertionError("label_names must be set")

    def get_gauge(
        self, key: str, unit: str = "", documentation: str | None = None
    ) -> GaugeMetricFamily:
        cache_key = f"{key}_{unit}" if unit else key
        if cache_key in self.metrics:
            metric = self.metrics[cache_key]
            if isinstance(metric, GaugeMetricFamily):
                return metric
            else:
                raise ValueError(f"Metric {key} already added as a {type(metric)}")
        else:
            new = GaugeMetricFamily(
                key,
                documentation or self.default_documentation,
                labels=self.label_names,
                unit=unit,
            )
            self.metrics[cache_key] = new
            return new

    def get_enum(
        self, key: str, documentation: str | None = None
    ) -> StateSetMetricFamily:
        if key in self.metrics:
            metric = self.metrics[key]
            if isinstance(metric, StateSetMetricFamily):
                return metric
            else:
                raise ValueError(f"Metric {key} already added as a {type(metric)}")
        else:
            new = StateSetMetricFamily(
                key,
                documentation or self.default_documentation,
                labels=self.label_names,
            )
            self.metrics[key] = new
            return new

    async def initialize(self) -> None:
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
            self.metrics = {}

            await self.collect_metrics()

            metrics = map(lambda x: x[1], sorted(self.metrics.items()))

            self.metrics = {}

            return metrics
        except Exception as exc:
            logger.warning("Metric collection failed", exc_info=exc)
            return []

    @abstractmethod
    async def collect_metrics(self):
        """
        Perform metric collection. Will be called once per minute.
        """
