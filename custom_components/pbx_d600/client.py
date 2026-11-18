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
        self._last_offhook: int | None = None
        self._await_bill: dict[int, tuple[dict, asyncio.TimerHandle | None]] = {}
        self._seen_bills: dict[bytes, float] = {}
        self._write_waiter: asyncio.Future | None = None
        self._commit_waiter: asyncio.Future | None = None
        self._write_lock = asyncio.Lock()
        self.forwarding: dict[int, dict] = {}

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

    # ------------------------------------------------------------ scrittura / deviazioni
    async def _send_wait(self, frame: bytes, attr: str, retries: int = 3) -> bool:
        loop = asyncio.get_running_loop()
        for _ in range(retries):
            fut = loop.create_future()
            setattr(self, attr, fut)
            self.send(frame)
            try:
                await asyncio.wait_for(fut, 2.0)
                return True
            except asyncio.TimeoutError:
                continue
            finally:
                setattr(self, attr, None)
        return False

    async def write_bytes(self, addr: int, data: bytes) -> bool:
        """Scrive byte per byte con conferma dopo ognuno, esattamente come il programma originale."""
        async with self._write_lock:
            for i, b in enumerate(data):
                if not await self._send_wait(p.build_write_byte(addr + i, b), "_write_waiter"):
                    _LOGGER.error("Scrittura 0x%04x non confermata dal centralino", addr + i)
                    return False
                if not await self._send_wait(p.build_commit(), "_commit_waiter"):
                    _LOGGER.error("Conferma scrittura 0x%04x non ricevuta", addr + i)
                    return False
            return True

    async def read_forwarding(self) -> dict[int, dict] | None:
        mem: dict[int, int] = {}
        for page in p.FWD_PAGES:
            data = await self.read_memory(page)
            if data is None:
                return None
            for i, b in enumerate(data):
                mem[page + i] = b
        out: dict[int, dict] = {}
        for port in range(1, self.num_ext + 1):
            mode_byte = mem[p.FWD_MODE_ADDR + port - 1]
            code = mode_byte >> 5
            dest = mem[p.FWD_DEST_ADDR + port - 1] + 1
            base = p.FWD_NUM_ADDR + (port - 1) * 16
            entry = bytes(mem[base + i] for i in range(16))
            number = p.decode_digits(entry[1:], entry[0]) if 0 < entry[0] <= 30 else ""
            out[port] = {
                "port": port,
                "extension": self.extensions.get(port, str(port)),
                "mode_code": code,
                "mode": p.FWD_MODES.get(code, f"sconosciuta_{code}"),
                "mode_byte": mode_byte,
                "target_port": dest,
                "target": self.extensions.get(dest, str(dest)) if code in (1, 2) else None,
                "number": number or None,
            }
        self.forwarding = out
        return out

    async def set_forwarding(self, port: int, code: int, target_port: int | None = None,
                             number: str | None = None) -> bool:
        """Imposta la deviazione di un interno. code: 0, 1, 2, 5, 6 (vedi protocol.FWD_MODES)."""
        if code not in p.FWD_MODES or not 1 <= port <= self.num_ext:
            raise ValueError("porta o modalità non valida")
        current = await self.read_forwarding()
        if current is None:
            return False
        if code in (1, 2):
            if not target_port or not 1 <= target_port <= self.num_ext or target_port == port:
                raise ValueError("interno di destinazione non valido")
            if not await self.write_bytes(p.FWD_DEST_ADDR + port - 1, bytes([target_port - 1])):
                return False
        if code in (5, 6):
            if not number or not number.isdigit():
                raise ValueError("numero esterno non valido")
            if not await self.write_bytes(p.FWD_NUM_ADDR + (port - 1) * 16, p.encode_number(number)):
                return False
        new_mode = (code << 5) | (current[port]["mode_byte"] & 0x1F)
        if not await self.write_bytes(p.FWD_MODE_ADDR + port - 1, bytes([new_mode])):
            return False
        await self.read_forwarding()
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

    def _finalize(self, call: dict) -> None:
        self.calls.insert(0, call)
        del self.calls[200:]
        if self.on_call_ended:
            self.on_call_ended(call)

    def _call_end(self, trunk: int) -> None:
        call = self._active.pop(trunk, None)
        if call is None:
            return
        end = self._now()
        call["fine"] = end.isoformat(timespec="seconds")
        if call["direzione"] == "uscente":
            # il riepilogo col numero composto arriva subito dopo: lo aspetto qualche secondo
            call["esito"] = "effettuata"
            if call["risposta"]:
                call["durata"] = int((end - datetime.fromisoformat(call["risposta"])).total_seconds())
            handle = None
            try:
                handle = asyncio.get_running_loop().call_later(8, self._bill_timeout, trunk)
            except RuntimeError:  # nessun loop (test)
                pass
            self._await_bill[trunk] = (call, handle)
            return
        if call["risposta"]:
            call["durata"] = int((end - datetime.fromisoformat(call["risposta"])).total_seconds())
            call["esito"] = "risposta"
        else:
            call["esito"] = "persa"
        self._finalize(call)

    def _bill_timeout(self, trunk: int) -> None:
        item = self._await_bill.pop(trunk, None)
        if item:
            self._finalize(item[0])

    def _on_bill(self, m: p.Message) -> None:
        self.send(p.build_bill_ack(m.address))
        if not m.start or m.start[0] == 0:
            return  # record vuoto/di servizio
        now = time.monotonic()
        self._seen_bills = {k: t for k, t in self._seen_bills.items() if now - t < 120}
        if m.raw in self._seen_bills:  # ripetizione dello stesso riepilogo
            return
        self._seen_bills[m.raw] = now
        if m.direction not in (0, None):
            return  # riepilogo di chiamata entrante: già registrata dagli eventi
        item = self._await_bill.pop(m.trunk, None)
        if item:
            call, handle = item
            if handle:
                handle.cancel()
        else:
            n = self._now()
            mo, d, h, mi = m.start
            call = {
                "direzione": "uscente", "linea": m.trunk, "numero": None, "interno": None,
                "inizio": n.replace(month=mo, day=d, hour=h, minute=mi, second=0, microsecond=0).isoformat(timespec="seconds"),
                "risposta": None, "fine": n.isoformat(timespec="seconds"), "durata": 0, "esito": "effettuata",
            }
        call["numero"] = m.number or call["numero"]
        call["interno"] = self._ext(m.port) or call["interno"]
        call["durata"] = m.duration
        self._finalize(call)

    def _track_calls(self, m: p.Message) -> None:
        k = m.kind
        if k == "off_hook":
            self._last_offhook = m.port
        elif k == "trunk_seized":
            call = self._call_start(m.trunk, "uscente")
            call["interno"] = self._ext(self._last_offhook)
        elif k == "trunk_ringing" and m.trunk not in self._active:
            self._call_start(m.trunk, "entrante")
        elif k == "caller_id":
            call = self._active.get(m.trunk) or self._call_start(m.trunk, "entrante")
            call["numero"] = m.number
        elif k == "trunk_connected":
            call = self._active.get(m.trunk)
            if call is None:
                call = self._call_start(m.trunk, "uscente")
            call["interno"] = self._ext(m.port)
            if call["risposta"] is None:
                call["risposta"] = self._now().isoformat(timespec="seconds")
        elif k == "trunk_released":
            self._call_end(m.trunk)
        elif k == "call_record":
            self._on_bill(m)
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
        elif k == "write_ack":
            if self._write_waiter and not self._write_waiter.done():
                self._write_waiter.set_result(True)
        elif k == "commit_ack":
            if self._commit_waiter and not self._commit_waiter.done():
                self._commit_waiter.set_result(True)
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
        elif k == "trunk_seized":
            if m.trunk in self.trunk_state:
                self.trunk_state[m.trunk] = TRUNK_BUSY
            if self._last_offhook in self.ext_state:
                self.ext_state[self._last_offhook] = EXT_EXTERNAL
                self.ext_trunk[self._last_offhook] = m.trunk
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
            self.send(p.build_cid_ack(m.trunk_raw))
            if m.trunk in self.trunk_cid:
                self.trunk_cid[m.trunk] = m.number
            self.last_cid = m.number
            self.last_cid_trunk = m.trunk
            self.last_cid_time = self._now()
