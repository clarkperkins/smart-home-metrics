from prometheus_client import CollectorRegistry, make_wsgi_app

from shm.logging import setup_logging
from shm.metrics import SmartHomeCollector


def build_app():
    setup_logging()
    registry = CollectorRegistry(auto_describe=True)

    registry.register(SmartHomeCollector())

    return make_wsgi_app(registry)


app = build_app()
