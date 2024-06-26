import logging
from collections import defaultdict
from collections.abc import Iterable

import anyio
from aiohttp import ClientSession
from pydantic_settings import BaseSettings
from pysmartthings import DeviceEntity, LocationEntity, RoomEntity, SmartThings

from shm.collectors import MetricCollector

logger = logging.getLogger(__name__)

EXCLUDED_DEVICE_NAMES = [
    "v4 - ecobee Thermostat - Heat and Cool (F)",
    "ecobee Sensor",
]


class SmartThingsConfig(BaseSettings):
    token: str

    class Config:
        env_prefix = "SMARTTHINGS_"


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
    default_documentation = "SmartThings Device"

    def __init__(self, session: ClientSession):
        super().__init__(session)

        self.config = SmartThingsConfig()

        self.api = SmartThings(session, self.config.token)

    async def lookup_locations(self) -> dict[str, LocationEntity]:
        locations = await self.api.locations()

        location_lookup = {}
        for location in locations:
            location_lookup[location.location_id] = location

        return location_lookup

    @staticmethod
    async def lookup_rooms(
        locations: Iterable[LocationEntity],
    ) -> dict[str, dict[str, RoomEntity]]:
        rooms: list[RoomEntity] = []

        async def _save_room(loc: LocationEntity):
            rooms.extend(await loc.rooms())

        async with anyio.create_task_group() as group:
            for location in locations:
                group.start_soon(_save_room, location)

        room_lookup: dict[str, dict[str, RoomEntity]] = defaultdict(dict)
        for room in rooms:
            room_lookup[room.location_id][room.room_id] = room

        return room_lookup

    async def collect_metrics(self):
        logger.debug("Collecting smartthings metrics...")

        devices = await self.api.devices()
        locations = await self.lookup_locations()
        rooms = await self.lookup_rooms(locations.values())

        device_metrics = [
            DeviceMetric(self, self.api, d, locations, rooms)
            for d in devices
            if d.name not in EXCLUDED_DEVICE_NAMES
        ]

        async with anyio.create_task_group() as group:
            for d in device_metrics:
                group.start_soon(d.get_metrics)


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

    def get_labels(self) -> list[str]:
        # Must match the order defined on the collector above
        return [
            self.device.device_id or "",
            self.device.name or "",
            self.device.label or "",
            self.device.location_id or "",
            self.location.name if self.location else None or "",
            self.device.room_id or "",
            self.room.name if self.room else None or "",
            self.device.type or "",
            self.device.device_type_id or "",
            self.device.device_type_name or "",
            self.device.device_type_network or "",
        ]

    async def get_metrics(self):
        status = await self.api._service.get_device_status(self.device.device_id)
        components = status.get("components", {})

        labels = self.get_labels()

        for component, capabilities in components.items():
            for capability, attributes in capabilities.items():
                for attribute, data in attributes.items():
                    if attribute in self.ignore:
                        continue

                    key = f"smartthings_{component}_{capability}_{attribute}".replace(
                        "-", "_"
                    ).replace(".", "_")
                    value = data.get("value")

                    if isinstance(value, (int, float)):
                        unit = data.get("unit")
                        if unit == "%":
                            unit = "pct"

                        self.collector.get_gauge(key, unit=unit).add_metric(
                            labels, value
                        )
                    elif attribute in self.enums:
                        if value:
                            e = self.collector.get_enum(key)
                            e.add_metric(
                                labels,
                                {
                                    state: state == value
                                    for state in self.enums[attribute]
                                },
                            )
                    elif attribute == "threeAxis":
                        self.collector.get_gauge(key, unit="x").add_metric(
                            labels, value[0]
                        )
                        self.collector.get_gauge(key, unit="y").add_metric(
                            labels, value[1]
                        )
                        self.collector.get_gauge(key, unit="z").add_metric(
                            labels, value[2]
                        )
                    elif attribute == "thermostatFanMode":
                        if value:
                            modes = attributes["supportedThermostatFanModes"]["value"]
                            e = self.collector.get_enum(key)
                            e.add_metric(
                                labels, {mode: mode == value for mode in modes}
                            )
                    elif attribute == "thermostatMode":
                        if value:
                            modes = attributes["supportedThermostatModes"]["value"]
                            e = self.collector.get_enum(key)
                            e.add_metric(
                                labels, {mode: mode == value for mode in modes}
                            )
                    # else:
                    #     print(f"{key} = {data}")
