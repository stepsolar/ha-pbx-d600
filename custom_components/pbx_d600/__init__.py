"""Integrazione Home Assistant per centralino PBX D600 (comunicazione UDP diretta)."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import protocol as p
from .client import PbxClient
from .const import (
    CONF_LOCAL_PORT,
    CONF_NUM_EXT,
    CONF_NUM_TRUNKS,
    DOMAIN,
    EVENT_PBX,
    SIGNAL_UPDATE,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

type PbxConfigEntry = ConfigEntry[PbxClient]


async def async_setup_entry(hass: HomeAssistant, entry: PbxConfigEntry) -> bool:
    cfg = {**entry.data, **entry.options}
    client = PbxClient(
        cfg[CONF_HOST],
        cfg[CONF_PORT],
        cfg[CONF_LOCAL_PORT],
        cfg[CONF_NUM_EXT],
        cfg[CONF_NUM_TRUNKS],
    )
    try:
        await client.start()
    except OSError as err:
        raise HomeAssistantError(
            f"Impossibile aprire la porta UDP {cfg[CONF_LOCAL_PORT]}: {err}"
        ) from err

    if not await client.load_extension_numbers():
        _LOGGER.warning("Impossibile leggere i numeri interni: uso i numeri di porta")

    signal = SIGNAL_UPDATE.format(entry.entry_id)

    def _on_message(msg: p.Message) -> None:
        if msg.kind not in ("heartbeat", "clock_ack", "memory"):
            data = {"type": msg.kind, "raw": msg.raw.hex(" ")}
            if msg.port is not None:
                data["port"] = msg.port
                data["extension"] = client.extensions.get(msg.port)
            if msg.trunk is not None:
                data["trunk"] = msg.trunk
            if msg.number is not None:
                data["number"] = msg.number
            hass.bus.async_fire(EVENT_PBX, data)
            if msg.kind == "unknown":
                _LOGGER.info("Messaggio PBX non riconosciuto: %s", msg.raw.hex(" "))
        async_dispatcher_send(hass, signal)

    entry.async_on_unload(client.add_listener(_on_message))
    entry.runtime_data = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    _register_services(hass)
    return True


async def _reload(hass: HomeAssistant, entry: PbxConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PbxConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.stop()
    return ok


def _client(hass: HomeAssistant) -> PbxClient:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("Centralino PBX D600 non configurato")
    return entries[0].runtime_data


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "sync_clock"):
        return

    async def sync_clock(call: ServiceCall) -> None:
        if not await _client(hass).sync_clock():
            raise HomeAssistantError("Il centralino non ha risposto")

    async def read_memory(call: ServiceCall) -> ServiceResponse:
        addr = int(str(call.data["address"]), 16)
        data = await _client(hass).read_memory(addr)
        if data is None:
            raise HomeAssistantError("Il centralino non ha risposto")
        return {"address": f"{addr:04x}", "data": data.hex(" ")}

    async def send_raw(call: ServiceCall) -> None:
        body = bytes.fromhex(str(call.data["payload"]).replace(" ", ""))
        _client(hass).send(p.build(body))

    hass.services.async_register(DOMAIN, "sync_clock", sync_clock)
    hass.services.async_register(
        DOMAIN,
        "read_memory",
        read_memory,
        schema=vol.Schema({vol.Required("address"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "send_raw",
        send_raw,
        schema=vol.Schema({vol.Required("payload"): cv.string}),
    )
