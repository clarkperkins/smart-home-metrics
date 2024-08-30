from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import REGISTRY
from prometheus_fastapi_instrumentator import Instrumentator

from shm.metrics import SmartHomeCollector

instrumentator = Instrumentator()


@asynccontextmanager
async def lifespan(fastapi: FastAPI):
    collector = SmartHomeCollector()
    await collector.setup_collectors()

    REGISTRY.register(collector)

    instrumentator.expose(fastapi, True)

    yield


app = FastAPI(lifespan=lifespan)

instrumentator.instrument(app)


@app.get("/health")
async def health():
    return {"healthy": True}
