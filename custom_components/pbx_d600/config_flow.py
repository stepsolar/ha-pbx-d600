"""Config flow per PBX D600."""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from . import protocol as p
from .const import (
    CONF_LOCAL_PORT,
    CONF_NUM_EXT,
    CONF_NUM_TRUNKS,
    DEFAULT_LOCAL_PORT,
    DEFAULT_NUM_EXT,
    DEFAULT_NUM_TRUNKS,
    DEFAULT_PORT,
    DOMAIN,
)


class _Probe(asyncio.DatagramProtocol):
    def __init__(self, fut: asyncio.Future) -> None:
        self.fut = fut

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if not self.fut.done() and p.parse(data).kind == "clock_ack":
            self.fut.set_result(True)


async def _probe(host: str, port: int) -> bool:
    """Invia un'impostazione ora (comando innocuo) e attende l'ack."""
    from datetime import datetime

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _Probe(fut), remote_addr=(host, port)
    )
    try:
        for _ in range(3):
            transport.sendto(p.build_clock(datetime.now()))
            try:
                return await asyncio.wait_for(asyncio.shield(fut), 2)
            except asyncio.TimeoutError:
                continue
        return False
    finally:
        transport.close()


class PbxD600ConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            try:
                ok = await _probe(user_input[CONF_HOST], user_input[CONF_PORT])
            except OSError:
                ok = False
            if ok:
                return self.async_create_entry(
                    title=f"PBX D600 ({user_input[CONF_HOST]})", data=user_input
                )
            errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=""): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_LOCAL_PORT, default=DEFAULT_LOCAL_PORT): int,
                vol.Required(CONF_NUM_EXT, default=DEFAULT_NUM_EXT): vol.All(int, vol.Range(1, 256)),
                vol.Required(CONF_NUM_TRUNKS, default=DEFAULT_NUM_TRUNKS): vol.All(int, vol.Range(1, 32)),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
