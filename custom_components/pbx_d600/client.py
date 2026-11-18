"""Client UDP asincrono per il centralino PBX D600."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
import logging
import socket
import time

from . import protocol as p
from .const import (
    EXT_EXTERNAL,
    EXT_IDLE,
    EXT_OFFHOOK,
    EXT_RINGING,
    HEARTBEAT_INTERVAL,
    OFFLINE_AFTER,
    TRUNK_BUSY,
    TRUNK_IDLE,
    TRUNK_RINGING,
)

_LOGGER = logging.getLogger(__name__)


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, client: PbxClient) -> None:
        self.client = client

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        if addr[0] != self.client.host:
            return
        self.client._handle(data)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("Errore UDP: %s", exc)


class PbxClient:
    """Mantiene il collegamento e lo stato di interni e linee."""

    def __init__(
        self,
        host: str,
        port: int,
        local_port: int,
        num_ext: int,
        num_trunks: int,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.host = host
        self.port = port
        self.local_port = local_port
        self.num_ext = num_ext
        self.num_trunks = num_trunks
        self._now = now
        self._transport: asyncio.DatagramTransport | None = None
        self._hb_task: asyncio.Task | None = None
        self._listeners: list[Callable[[p.Message], None]] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._clock_waiter: asyncio.Future | None = None
        self.last_rx: float = 0.0

        self.extensions: dict[int, str] = {i: str(i) for i in range(1, num_ext + 1)}
        self.ext_state: dict[int, str] = {i: EXT_IDLE for i in range(1, num_ext + 1)}
        self.ext_trunk: dict[int, int | None] = {i: None for i in range(1, num_ext + 1)}
        self.trunk_state: dict[int, str] = {i: TRUNK_IDLE for i in range(1, num_trunks + 1)}
        self.trunk_cid: dict[int, str | None] = {i: None for i in range(1, num_trunks + 1)}
        self.last_cid: str | None = None
        self.last_cid_trunk: int | None = None
        self.last_cid_time: datetime | None = None

        # Registro chiamate
        self.calls: list[dict] = []  # più recenti prima
        self._active: dict[int, dict] = {}  # chiamate in corso per linea
        self.unknown_frames: list[str] = []
        self.on_call_ended: Callable[[dict], None] | None = None
        self.on_unknown: Callable[[], None] | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def connected(self) -> bool:
        return self.last_rx > 0 and (time.monotonic() - self.last_rx) < OFFLINE_AFTER

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # Socket con SO_REUSEADDR: una ricarica dell'integrazione non resta
        # bloccata dalla porta ancora in chiusura dell'istanza precedente.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.local_port))
            sock.connect((self.host, self.port))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._transport, _ = await loop.create_datagram_endpoint(lambda: _Proto(self), sock=sock)
        self._hb_task = loop.create_task(self._heartbeat())

    async def stop(self) -> None:
        if self._hb_task:
            self._hb_task.cancel()
            self._hb_task = None
        if self._transport:
            self._transport.close()
        self._transport = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()

    def add_listener(self, cb: Callable[[p.Message], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    # ------------------------------------------------------------ comandi
    def send(self, frame: bytes) -> None:
        if self._transport:
            _LOGGER.debug("TX %s", frame.hex(" "))
            self._transport.sendto(frame)

    async def sync_clock(self, timeout: float = 3.0) -> bool:
        loop = asyncio.get_running_loop()
        self._clock_waiter = loop.create_future()
        self.send(p.build_clock(self._now()))
        try:
            await asyncio.wait_for(self._clock_waiter, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._clock_waiter = None

    async def read_memory(self, addr: int, retries: int = 3) -> bytes | None:
        loop = asyncio.get_running_loop()
        for _ in range(retries):
            fut = loop.create_future()
            self._pending[addr] = fut
            self.send(p.build_read(addr))
            try:
                return await asyncio.wait_for(fut, 3.0)
            except asyncio.TimeoutError:
                continue
            finally:
                self._pending.pop(addr, None)
        return None

    async def load_extension_numbers(self) -> bool:
        buf = b""
        for page in p.EXT_TABLE_PAGES:
            data = await self.read_memory(page)
            if data is None:
                return False
            buf += data
        offset = p.EXT_TABLE_ADDR - p.EXT_TABLE_PAGES[0]
        table = p.decode_ext_table(buf[offset:], self.num_ext)
        self.extensions.update(table)
        _LOGGER.info("Numeri interni letti dal centralino: %s", table)
        return True

    # ------------------------------------------------------------ ricezione
    async def _heartbeat(self) -> None:
        while True:
            try:
                await self.sync_clock()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Errore heartbeat")
            for cb in list(self._listeners):
                cb(p.Message("heartbeat", b""))
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    def _handle(self, data: bytes) -> None:
        # Un datagramma può contenere più frame consecutivi
        i = 0
        while i < len(data):
            ln = data[i]
            if ln < 3 or i + ln > len(data):
                _LOGGER.debug("Frame troncato: %s", data[i:].hex(" "))
                break
            self._handle_frame(data[i : i + ln])
            i += ln

    def _handle_frame(self, frame: bytes) -> None:
        msg = p.parse(frame)
        self.last_rx = time.monotonic()
        _LOGGER.debug("RX %s -> %s", frame.hex(" "), msg.kind)
        self._apply(msg)
        for cb in list(self._listeners):
            cb(msg)

    # ------------------------------------------------------------ registro chiamate
    def _ext(self, port: int | None) -> str | None:
        return None if port is None else self.extensions.get(port, str(port))

    def _call_start(self, trunk: int, direction: str) -> dict:
        call = {
            "direzione": direction,  # "entrante" / "uscente"
            "linea": trunk,
            "numero": None,
            "interno": None,
            "inizio": self._now().isoformat(timespec="seconds"),
            "risposta": None,
            "fine": None,
            "durata": 0,
            "esito": None,
        }
        self._active[trunk] = call
        return call

    def _call_end(self, trunk: int) -> None:
        call = self._active.pop(trunk, None)
        if call is None:
            return
        end = self._now()
        call["fine"] = end.isoformat(timespec="seconds")
        if call["risposta"]:
            call["durata"] = int((end - datetime.fromisoformat(call["risposta"])).total_seconds())
            call["esito"] = "risposta" if call["direzione"] == "entrante" else "effettuata"
        else:
            call["esito"] = "persa" if call["direzione"] == "entrante" else "non risposta"
        self.calls.insert(0, call)
        del self.calls[200:]
        if self.on_call_ended:
            self.on_call_ended(call)

    def _track_calls(self, m: p.Message) -> None:
        k = m.kind
        if k == "trunk_ringing" and m.trunk not in self._active:
            self._call_start(m.trunk, "entrante")
        elif k == "caller_id":
            call = self._active.get(m.trunk) or self._call_start(m.trunk, "entrante")
            call["numero"] = m.number
        elif k == "trunk_connected":
            call = self._active.get(m.trunk)
            if call is None:
                # linea impegnata senza squillo prima: chiamata in uscita
                call = self._call_start(m.trunk, "uscente")
            call["interno"] = self._ext(m.port)
            if call["risposta"] is None:
                call["risposta"] = self._now().isoformat(timespec="seconds")
        elif k == "trunk_released":
            self._call_end(m.trunk)
        elif k == "unknown":
            self.unknown_frames.insert(0, f"{self._now().isoformat(timespec='seconds')} {m.raw.hex(' ')}")
            del self.unknown_frames[100:]
            if self.on_unknown:
                self.on_unknown()

    def _apply(self, m: p.Message) -> None:
        self._track_calls(m)
        k = m.kind
        if k == "clock_ack":
            if self._clock_waiter and not self._clock_waiter.done():
                self._clock_waiter.set_result(True)
        elif k == "memory":
            fut = self._pending.get(m.address)
            if fut and not fut.done():
                fut.set_result(m.data)
        elif k == "off_hook" and m.port in self.ext_state:
            if self.ext_state[m.port] != EXT_EXTERNAL:
                self.ext_state[m.port] = EXT_OFFHOOK
        elif k == "on_hook" and m.port in self.ext_state:
            self.ext_state[m.port] = EXT_IDLE
            self.ext_trunk[m.port] = None
            # chiamata interna abbandonata: il PBX non manda "fine squillo",
            # quindi se nessuna linea esterna squilla azzero gli interni in squillo
            if TRUNK_RINGING not in self.trunk_state.values():
                for port, st in self.ext_state.items():
                    if st == EXT_RINGING:
                        self.ext_state[port] = EXT_IDLE
        elif k == "ext_ringing" and m.port in self.ext_state:
            self.ext_state[m.port] = EXT_RINGING
        elif k == "trunk_ringing" and m.trunk in self.trunk_state:
            self.trunk_state[m.trunk] = TRUNK_RINGING
        elif k == "trunk_connected":
            if m.trunk in self.trunk_state:
                self.trunk_state[m.trunk] = TRUNK_BUSY
            if m.port in self.ext_state:
                self.ext_state[m.port] = EXT_EXTERNAL
                self.ext_trunk[m.port] = m.trunk
        elif k == "trunk_released" and m.trunk in self.trunk_state:
            self.trunk_state[m.trunk] = TRUNK_IDLE
            self.trunk_cid[m.trunk] = None
            # gli interni che squillavano per questa chiamata tornano liberi
            for port, st in self.ext_state.items():
                if st == EXT_RINGING:
                    self.ext_state[port] = EXT_IDLE
        elif k == "caller_id":
            self.send(p.build_cid_ack(m.trunk))
            if m.trunk in self.trunk_cid:
                self.trunk_cid[m.trunk] = m.number
            self.last_cid = m.number
            self.last_cid_trunk = m.trunk
            self.last_cid_time = self._now()
