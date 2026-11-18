"""Sensori: stato interni, stato linee, ultimo chiamante."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CALL_LIST_SIZE, EXT_EXTERNAL, EXT_IDLE, EXT_OFFHOOK, EXT_RINGING, TRUNK_BUSY, TRUNK_IDLE, TRUNK_RINGING
from .entity import PbxEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    c = entry.runtime_data
    ents: list[SensorEntity] = [
        LastCaller(entry),
        CallLog(entry, "received", "Chiamate ricevute", "mdi:phone-incoming", "entrante", "risposta"),
        CallLog(entry, "missed", "Chiamate perse", "mdi:phone-missed", "entrante", "persa"),
        CallLog(entry, "outgoing", "Chiamate effettuate", "mdi:phone-outgoing", "uscente", None),
        UnknownFrames(entry),
    ]
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


class CallLog(PbxEntity, SensorEntity):
    """Stato = chiamate di oggi; attributo `chiamate` = ultime chiamate (più recenti prima)."""

    _unrecorded_attributes = frozenset({"chiamate"})
    _attr_native_unit_of_measurement = "chiamate oggi"

    def __init__(self, entry, key: str, name: str, icon: str, direction: str, outcome: str | None) -> None:
        super().__init__(entry, f"calls_{key}")
        self._attr_name = name
        self._attr_icon = icon
        self.direction = direction
        self.outcome = outcome

    def _calls(self) -> list[dict]:
        return [
            c for c in self.client.calls
            if c["direzione"] == self.direction and (self.outcome is None or c["esito"] == self.outcome)
        ]

    @property
    def native_value(self) -> int:
        today = dt_util.now().date().isoformat()
        return sum(1 for c in self._calls() if c["inizio"].startswith(today))

    @property
    def extra_state_attributes(self) -> dict:
        calls = self._calls()
        return {"totale": len(calls), "chiamate": calls[:CALL_LIST_SIZE]}


class UnknownFrames(PbxEntity, SensorEntity):
    """Messaggi del centralino non ancora decifrati (servono per estendere l'integrazione)."""

    _unrecorded_attributes = frozenset({"messaggi"})
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:help-network"
    _attr_name = "Messaggi non riconosciuti"

    def __init__(self, entry) -> None:
        super().__init__(entry, "unknown_frames")

    @property
    def native_value(self) -> int:
        return len(self.client.unknown_frames)

    @property
    def extra_state_attributes(self) -> dict:
        return {"messaggi": self.client.unknown_frames}
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
