import asyncio
import logging
import logging.config
from pathlib import Path

import click
import yaml
from prometheus_client import start_http_server

from shm.metrics import collect_metrics

BASE_DIR = Path(__file__).resolve().parent.parent

logger = logging.getLogger(__name__)


@click.group(name="shm")
def main():
    pass


@main.command()
@click.option("--address", default="0.0.0.0")
@click.option("--port", "-p", type=int, default=9000)
def run(address: str, port: int):
    logging_config_file = BASE_DIR / "logging.yaml"
    if logging_config_file.is_file():
        with logging_config_file.open("rt", encoding="utf8") as f:
            logging.config.dictConfig(yaml.safe_load(f))

    start_http_server(port=port, addr=address)

    logger.info("Started prometheus metrics server at %s:%d", address, port)

    asyncio.run(collect_metrics())


if __name__ == "__main__":
    main()
