import asyncio
import logging
import os
from collections import defaultdict
from collections.abc import Iterable

from aiohttp import ClientSession
from prometheus_client import CollectorRegistry
from prometheus_client.metrics import T
from pysmartthings import DeviceEntity, LocationEntity, RoomEntity, SmartThings

from shm.collectors import MetricCollector

logger = logging.getLogger(__name__)


class SmartThingsMetricCollector(MetricCollector):
    label_names = [
        "device_id",
        "device_name",
        "device_label",
        "location_id",
        "location_name",
        "room_id",
        "room_name",
        "type",
        "device_type_id",
        "device_type_name",
        "device_type_network",
    ]

    def __init__(self, registry: CollectorRegistry, session: ClientSession):
        super().__init__(registry, session)

        token = os.environ.get("SMARTTHINGS_TOKEN")

        if not token:
            raise EnvironmentError("Missing SMARTTHINGS_TOKEN env var")

        self.api = SmartThings(session, token)

    async def lookup_locations(
        self,
    ) -> dict[str, LocationEntity]:
        locations = await self.api.locations()

        location_lookup = {}
        for location in locations:
            location_lookup[location.location_id] = location

        return location_lookup

    @staticmethod
    async def lookup_rooms(
        locations: Iterable[LocationEntity],
    ) -> dict[str, dict[str, RoomEntity]]:
        room_coros = [location.rooms() for location in locations]

        location_rooms = await asyncio.gather(*room_coros)

        room_lookup: dict[str, dict[str, RoomEntity]] = defaultdict(dict)
        for rooms in location_rooms:
            for room in rooms:
                room_lookup[room.location_id][room.room_id] = room

        return room_lookup

    async def collect_metrics(self):
        logger.debug("Collecting smartthings metrics...")

        devices = await self.api.devices()
        locations = await self.lookup_locations()
        rooms = await self.lookup_rooms(locations.values())

        device_metrics = [
            DeviceMetric(self, self.api, d, locations, rooms) for d in devices
        ]

        await asyncio.gather(*[d.update_metrics() for d in device_metrics])


class DeviceMetric:
    ignore = {
        "DeviceWatch-Enroll",
        "healthStatus",
    }

    enums = {
        "DeviceWatch-DeviceStatus": ("offline", "online"),
        "acceleration": ("inactive", "active"),
        "contact": ("closed", "open"),
        "motion": ("inactive", "active"),
        "mute": ("unmuted", "muted"),
        "occupancy": ("unoccupied", "occupied"),
        "presence": ("not present", "present"),
        "switch": ("off", "on"),
        "water": ("dry", "wet"),
    }

    def __init__(
        self,
        collector: SmartThingsMetricCollector,
        api: SmartThings,
        device: DeviceEntity,
        locations: dict[str, LocationEntity],
        rooms: dict[str, dict[str, RoomEntity]],
    ):
        self.collector = collector
        self.api = api
        self.device = device
        self.location = locations.get(device.location_id)
        self.room = rooms.get(device.location_id, {}).get(device.room_id)

    def add_labels(self, metric: T) -> T:
        return metric.labels(
            device_id=self.device.device_id,
            device_name=self.device.name,
            device_label=self.device.label,
            location_id=self.device.location_id,
            location_name=self.location.name if self.location else None,
            room_id=self.device.room_id,
            room_name=self.room.name if self.room else None,
            type=self.device.type,
            device_type_id=self.device.device_type_id,
            device_type_name=self.device.device_type_name,
            device_type_network=self.device.device_type_network,
        )

    async def update_metrics(self):
        status = await self.api._service.get_device_status(self.device.device_id)
        components = status.get("components", {})

        for component, capabilities in components.items():
            for capability, attributes in capabilities.items():
                for attribute, data in attributes.items():
                    if attribute in self.ignore:
                        continue

                    key = f"smartthings_{component}_{capability}_{attribute}".replace(
                        "-", "_"
                    )
                    value = data.get("value")

                    if isinstance(value, (int, float)):
                        unit = data.get("unit")
                        if unit:
                            key = f"{key}_{unit}".replace("%", "pct")
                        g = self.add_labels(self.collector.get_gauge(key))
                        g.set(value)
                    elif attribute in self.enums:
                        if value:
                            e = self.add_labels(
                                self.collector.get_enum(key, self.enums[attribute])
                            )
                            e.state(value)
                    elif attribute == "threeAxis":
                        self.add_labels(self.collector.get_gauge(f"{key}_x")).set(
                            value[0]
                        )
                        self.add_labels(self.collector.get_gauge(f"{key}_y")).set(
                            value[1]
                        )
                        self.add_labels(self.collector.get_gauge(f"{key}_z")).set(
                            value[2]
                        )
                    elif attribute == "thermostatFanMode":
                        if value:
                            modes = attributes["supportedThermostatFanModes"]["value"]
                            e = self.add_labels(
                                self.collector.get_enum(
                                    key,
                                    tuple(modes),
                                )
                            )
                            e.state(value)
                    elif attribute == "thermostatMode":
                        if value:
                            modes = attributes["supportedThermostatModes"]["value"]
                            e = self.add_labels(
                                self.collector.get_enum(
                                    key,
                                    tuple(modes),
                                )
                            )
                            e.state(value)
                    # else:
                    #     print(f"{key} = {data}")
