import asyncio

import click

from shm.logging import setup_logging
from shm.metrics import Collector


@click.group(name="shm")
def main():
    pass


@main.command()
@click.option("--address", default="0.0.0.0")
@click.option("--port", "-p", type=int, default=9000)
def run(address: str, port: int):
    setup_logging()
    collector = Collector(address, port)
    asyncio.run(collector.run())


if __name__ == "__main__":
    main()
