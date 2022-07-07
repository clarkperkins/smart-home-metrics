import asyncio

import click
from prometheus_client import start_http_server

from stpm.metrics import collect_metrics


@click.group(name="stpm")
def main():
    pass


@main.command()
@click.option("--address", default="0.0.0.0")
@click.option("--port", "-p", type=int, default=9000)
def run(address: str, port: int):
    start_http_server(port=port, addr=address)
    asyncio.run(collect_metrics())


if __name__ == "__main__":
    main()
