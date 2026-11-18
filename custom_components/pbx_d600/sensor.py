"""Sensori: stato interni, stato linee, ultimo chiamante."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EXT_EXTERNAL, EXT_IDLE, EXT_OFFHOOK, EXT_RINGING, TRUNK_BUSY, TRUNK_IDLE, TRUNK_RINGING
from .entity import PbxEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    c = entry.runtime_data
    ents: list[SensorEntity] = [LastCaller(entry)]
    ents += [ExtensionSensor(entry, port) for port in range(1, c.num_ext + 1)]
    ents += [TrunkSensor(entry, t) for t in range(1, c.num_trunks + 1)]
    async_add_entities(ents)


class ExtensionSensor(PbxEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [EXT_IDLE, EXT_OFFHOOK, EXT_RINGING, EXT_EXTERNAL]
    _attr_icon = "mdi:phone-classic"

    def __init__(self, entry, port: int) -> None:
        super().__init__(entry, f"ext_port_{port}")
        self.port = port

    @property
    def name(self) -> str:
        return f"Interno {self.client.extensions.get(self.port, self.port)}"

    @property
    def native_value(self) -> str:
        return self.client.ext_state[self.port]

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "porta": self.port,
            "numero_interno": self.client.extensions.get(self.port),
            "linea_esterna": self.client.ext_trunk.get(self.port),
        }


class TrunkSensor(PbxEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [TRUNK_IDLE, TRUNK_RINGING, TRUNK_BUSY]
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, entry, trunk: int) -> None:
        super().__init__(entry, f"trunk_{trunk}")
        self.trunk = trunk
        self._attr_name = f"Linea esterna {trunk}"

    @property
    def native_value(self) -> str:
        return self.client.trunk_state[self.trunk]

    @property
    def extra_state_attributes(self) -> dict:
        return {"chiamante": self.client.trunk_cid.get(self.trunk)}


class LastCaller(PbxEntity, SensorEntity):
    _attr_icon = "mdi:card-account-phone"
    _attr_name = "Ultimo chiamante"

    def __init__(self, entry) -> None:
        super().__init__(entry, "last_caller")

    @property
    def native_value(self) -> str | None:
        return self.client.last_cid

    @property
    def extra_state_attributes(self) -> dict:
        t = self.client.last_cid_time
        return {"linea": self.client.last_cid_trunk, "ora": t.isoformat() if t else None}
