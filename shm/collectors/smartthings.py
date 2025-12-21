import logging
from collections import defaultdict
from collections.abc import Iterable

import anyio
from aiohttp import ClientSession
from pydantic_settings import BaseSettings
from pysmartthings import BaseLocation, Device, Room, SmartThings

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
        "device_network_type",
    ]
    default_documentation = "SmartThings Device"

    def __init__(self, session: ClientSession):
        super().__init__(session)

        self.config = SmartThingsConfig()

        self.api = SmartThings(session=session)
        self.api.authenticate(self.config.token)

    async def lookup_locations(self) -> dict[str, BaseLocation]:
        locations = await self.api.get_locations()

        location_lookup = {}
        for location in locations:
            location_lookup[location.location_id] = location

        return location_lookup

    async def lookup_rooms(
        self,
        locations: Iterable[BaseLocation],
    ) -> dict[str, dict[str, Room]]:
        rooms: list[Room] = []

        async def _save_room(loc: BaseLocation):
            rooms.extend(await self.api.get_rooms(loc.location_id))

        async with anyio.create_task_group() as group:
            for location in locations:
                group.start_soon(_save_room, location)

        room_lookup: dict[str, dict[str, Room]] = defaultdict(dict)
        for room in rooms:
            room_lookup[room.location_id][room.room_id] = room

        return room_lookup

    async def collect_metrics(self):
        logger.debug("Collecting smartthings metrics...")

        devices = await self.api.get_devices()
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
        device: Device,
        locations: dict[str, BaseLocation],
        rooms: dict[str, dict[str, Room]],
    ):
        self.collector = collector
        self.api = api
        self.device = device
        self.location = locations.get(device.location_id)
        self.room: Room | None = None
        if device.room_id:
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
            self.device.device_network_type or "",
        ]

    async def get_metrics(self):
        components = await self.api.get_device_status(self.device.device_id)

        labels = self.get_labels()

        for component, capabilities in components.items():
            for capability, attributes in capabilities.items():
                for attribute, data in attributes.items():
                    if attribute in self.ignore:
                        continue

                    key = f"smartthings_{component}_{capability}_{attribute}".replace(
                        "-", "_"
                    ).replace(".", "_")
                    value = data.value

                    if isinstance(value, (int, float)):
                        unit = data.unit or ""
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
                        assert isinstance(value, list)
                        x, y, z = value
                        self.collector.get_gauge(key, unit="x").add_metric(labels, x)
                        self.collector.get_gauge(key, unit="y").add_metric(labels, y)
                        self.collector.get_gauge(key, unit="z").add_metric(labels, z)
                    elif attribute == "thermostatFanMode":
                        if value:
                            modes = attributes["supportedThermostatFanModes"].value
                            assert isinstance(modes, list)
                            e = self.collector.get_enum(key)
                            e.add_metric(
                                labels, {mode: mode == value for mode in modes}
                            )
                    elif attribute == "thermostatMode":
                        if value:
                            modes = attributes["supportedThermostatModes"].value
                            assert isinstance(modes, list)
                            e = self.collector.get_enum(key)
                            e.add_metric(
                                labels, {mode: mode == value for mode in modes}
                            )
                    # else:
                    #     print(f"{key} = {data}")
