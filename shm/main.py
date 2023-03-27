from fastapi import FastAPI
from prometheus_client import REGISTRY
from prometheus_fastapi_instrumentator import Instrumentator

from shm.metrics import SmartHomeCollector

app = FastAPI()

instrumentator = Instrumentator()
instrumentator.instrument(app)
instrumentator.expose(app, True)


@app.on_event("startup")
async def startup():
    collector = SmartHomeCollector()
    await collector.setup_collectors()

    REGISTRY.register(collector)


@app.get("/health")
async def health():
    return {"healthy": True}
