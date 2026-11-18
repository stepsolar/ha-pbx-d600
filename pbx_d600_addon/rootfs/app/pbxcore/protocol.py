"""Protocollo UDP del centralino PBX D600 (ricostruito da cattura Wireshark).

Formato frame:  [LEN] [CMD] [dati...] [CHK]
  LEN = lunghezza totale del frame (byte LEN e CHK inclusi)
  CHK = XOR di tutti i byte precedenti

Comandi conosciuti
  PC  -> PBX  03 h h m m s s           imposta ora (cifre singole), ogni 30 s
  PBX -> PC   03 00 00                 ack ora (frame 05 03 00 00 06)
  PC  -> PBX  04 01 AH AL 80           leggi 128 byte di memoria a AH:AL
  PBX -> PC   04 01 AH AL 80 <128 B>   risposta lettura
  PBX -> PC   10 P                     interno porta P sganciato
  PBX -> PC   11 P                     interno porta P agganciato
  PBX -> PC   14 P                     interno porta P squilla
  PBX -> PC   17 T                     linea esterna T squilla (chiamata entrante)
  PBX -> PC   18 T                     linea esterna T liberata
  PBX -> PC   80 T P                   linea T collegata all'interno P (da confermare)
  PBX -> PC   08 T 01 N <cifre BCD>    caller ID su linea T (N cifre)
  PC  -> PBX  08 T                     ack caller ID
  PBX -> PC   19 T                     linea T impegnata da un interno (chiamata in uscita)
  PBX -> PC   01 T xx P D MM GG hh mm  dh dm ds <8 byte cifre> <3 byte>
                                       riepilogo/addebito di fine chiamata (BCD);
                                       ripetuto finché non arriva l'ack
  PC  -> PBX  01 T                     ack riepilogo (dedotto per analogia con 08 T)

Le linee esterne nei messaggi partono da 0 (T=0 -> linea 1); le porte interni da 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

CMD_CLOCK = 0x03
CMD_READ = 0x04
CMD_CID = 0x08
CMD_OFFHOOK = 0x10
CMD_ONHOOK = 0x11
CMD_EXT_RING = 0x14
CMD_TRUNK_RING = 0x17
CMD_TRUNK_FREE = 0x18
CMD_CONNECT = 0x80
CMD_TRUNK_SEIZE = 0x19
CMD_BILL = 0x01

# Tabella numeri interni: 4 byte per porta, a partire da 0x0300
EXT_TABLE_ADDR = 0x0300
EXT_TABLE_PAGES = (0x02A0, 0x0320)


def checksum(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x


def build(body: bytes) -> bytes:
    pkt = bytes([len(body) + 2]) + body
    return pkt + bytes([checksum(pkt)])


def build_clock(now: datetime) -> bytes:
    d = f"{now.hour:02d}{now.minute:02d}{now.second:02d}"
    return build(bytes([CMD_CLOCK]) + bytes(int(c) for c in d))


def build_read(addr: int) -> bytes:
    return build(bytes([CMD_READ, 0x01, (addr >> 8) & 0xFF, addr & 0xFF, 0x80]))


def build_cid_ack(trunk_raw: int) -> bytes:
    return build(bytes([CMD_CID, trunk_raw]))


def build_bill_ack(trunk_raw: int) -> bytes:
    return build(bytes([CMD_BILL, trunk_raw]))


def bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


def decode_digits(data: bytes, max_digits: int | None = None) -> str:
    """Cifre BCD con nibble invertiti; 0xD = fine, 0xA = 0."""
    out = ""
    for b in data:
        for n in (b & 0x0F, b >> 4):
            if n == 0x0D or (max_digits is not None and len(out) >= max_digits):
                return out
            if n < 10:
                out += str(n)
            elif n == 0x0A:
                out += "0"
            elif n == 0x0B:
                out += "*"
            elif n == 0x0C:
                out += "#"
            else:
                return out
    return out


def valid(frame: bytes) -> bool:
    return (
        len(frame) >= 3
        and frame[0] == len(frame)
        and checksum(frame[:-1]) == frame[-1]
    )


@dataclass
class Message:
    kind: str
    raw: bytes
    port: int | None = None
    trunk: int | None = None  # 1-based (linea 1 = primo trunk)
    trunk_raw: int | None = None
    duration: int | None = None  # secondi
    start: tuple[int, int, int, int] | None = None  # mese, giorno, ora, minuti
    direction: int | None = None
    number: str | None = None
    address: int | None = None
    data: bytes = field(default=b"", repr=False)


def parse(frame: bytes) -> Message:
    if not valid(frame):
        return Message("invalid", frame)
    cmd, args = frame[1], frame[2:-1]
    if cmd == CMD_CLOCK:
        return Message("clock_ack", frame)
    if cmd == CMD_READ and len(args) >= 4:
        return Message("memory", frame, address=(args[1] << 8) | args[2], data=bytes(args[4:]))
    if cmd == CMD_OFFHOOK and args:
        return Message("off_hook", frame, port=args[0])
    if cmd == CMD_ONHOOK and args:
        return Message("on_hook", frame, port=args[0])
    if cmd == CMD_EXT_RING and args:
        return Message("ext_ringing", frame, port=args[0])
    if cmd == CMD_TRUNK_RING and args:
        return Message("trunk_ringing", frame, trunk=args[0] + 1, trunk_raw=args[0])
    if cmd == CMD_TRUNK_FREE and args:
        return Message("trunk_released", frame, trunk=args[0] + 1, trunk_raw=args[0])
    if cmd == CMD_TRUNK_SEIZE and args:
        return Message("trunk_seized", frame, trunk=args[0] + 1, trunk_raw=args[0])
    if cmd == CMD_CONNECT and len(args) >= 2:
        return Message("trunk_connected", frame, trunk=args[0] + 1, trunk_raw=args[0], port=args[1])
    if cmd == CMD_CID and len(args) >= 3:
        # args: linea, tipo (01 osservato), numero cifre, cifre BCD
        return Message(
            "caller_id", frame, trunk=args[0] + 1, trunk_raw=args[0],
            number=decode_digits(args[3:], args[2]),
        )
    if cmd == CMD_BILL and len(args) >= 19:
        # 0 linea | 1 ? (contatore?) | 2 porta interno | 3 direzione? |
        # 4-7 MM GG hh mm | 8-10 durata hh mm ss | 11-18 numero | 19.. ?
        return Message(
            "call_record", frame, trunk=args[0] + 1, trunk_raw=args[0], port=args[2],
            direction=args[3],
            start=(bcd(args[4]), bcd(args[5]), bcd(args[6]), bcd(args[7])),
            duration=bcd(args[8]) * 3600 + bcd(args[9]) * 60 + bcd(args[10]),
            number=decode_digits(args[11:19]),
        )
    return Message("unknown", frame)


def decode_ext_table(buf: bytes, ports: int) -> dict[int, str]:
    out: dict[int, str] = {}
    for p in range(ports):
        num = decode_digits(buf[p * 4 : p * 4 + 4])
        if num:
            out[p + 1] = num
    return out
