"""Add-on PBX D600: collegamento UDP al centralino, plancia web (ingress) e entità Home Assistant."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

FWD_LABELS = {
    0: "Non trasferire",
    1: "Inoltra all'interno",
    2: "Trasferimento all'interno se occupato",
    5: "Trasferimento alla linea esterna",
    6: "Trasferimento linea esterna se occupato",
}
FWD_KEYS = {"nessuna": 0, "interno": 1, "interno_occupato": 2, "esterno": 5, "esterno_occupato": 6}

from pbxcore import protocol as p
from pbxcore.client import PbxClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger("pbx_d600")

DATA = Path(os.environ.get("PBX_DATA", "/data"))
WEB = Path(__file__).parent / "web"
PORT = int(os.environ.get("PBX_WEB_PORT", "8099"))
VERSION = "1.1.0"


def load_options() -> dict:
    defaults = {
        "host": "", "port": 50001, "local_port": 50000,
        "num_extensions": 32, "num_trunks": 8, "publish_entities": True,
    }
    try:
        defaults.update(json.loads((DATA / "options.json").read_text()))
    except FileNotFoundError:
        pass
    return defaults


# --------------------------------------------------------------------------- database
class Db:
    def __init__(self, path: Path) -> None:
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direzione TEXT, linea INTEGER, numero TEXT, interno TEXT,
                inizio TEXT, risposta TEXT, fine TEXT, durata INTEGER, esito TEXT
            );
            CREATE INDEX IF NOT EXISTS calls_inizio ON calls(inizio);
            CREATE TABLE IF NOT EXISTS names (
                kind TEXT, key TEXT, name TEXT, PRIMARY KEY (kind, key)
            );
            CREATE TABLE IF NOT EXISTS unknown (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, raw TEXT
            );
            """
        )
        self.con.commit()

    def add_call(self, c: dict) -> None:
        self.con.execute(
            "INSERT INTO calls (direzione, linea, numero, interno, inizio, risposta, fine, durata, esito)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (c["direzione"], c["linea"], c["numero"], c["interno"], c["inizio"],
             c["risposta"], c["fine"], c["durata"], c["esito"]),
        )
        self.con.commit()

    def calls(self, esito: str | None, q: str | None, day_from: str | None, day_to: str | None,
              limit: int, offset: int) -> tuple[list[dict], int]:
        where, args = [], []
        if esito == "effettuata":
            where.append("direzione = 'uscente'")
        elif esito:
            where.append("esito = ?")
            args.append(esito)
        if q:
            names = [k for k, v in self.names("contact").items() if q.lower() in v.lower()]
            cond = "(numero LIKE ? OR interno LIKE ?"
            args += [f"%{q}%", f"%{q}%"]
            if names:
                cond += f" OR numero IN ({','.join('?' * len(names))})"
                args += names
            where.append(cond + ")")
        if day_from:
            where.append("inizio >= ?")
            args.append(day_from)
        if day_to:
            where.append("inizio < ?")
            args.append((datetime.fromisoformat(day_to) + timedelta(days=1)).date().isoformat())
        sql_where = f"WHERE {' AND '.join(where)}" if where else ""
        total = self.con.execute(f"SELECT COUNT(*) FROM calls {sql_where}", args).fetchone()[0]
        rows = self.con.execute(
            f"SELECT * FROM calls {sql_where} ORDER BY inizio DESC, id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], total

    def today_counts(self) -> dict:
        today = datetime.now().date().isoformat()
        rows = self.con.execute(
            "SELECT direzione, esito, COUNT(*) n FROM calls WHERE inizio >= ? GROUP BY direzione, esito",
            (today,),
        ).fetchall()
        out = {"risposta": 0, "persa": 0, "effettuata": 0}
        for r in rows:
            if r["direzione"] == "uscente":
                out["effettuata"] += r["n"]
            elif r["esito"] in out:
                out[r["esito"]] += r["n"]
        return out

    def names(self, kind: str) -> dict[str, str]:
        return {r["key"]: r["name"] for r in self.con.execute("SELECT key, name FROM names WHERE kind=?", (kind,))}

    def set_name(self, kind: str, key: str, name: str) -> None:
        if name.strip():
            self.con.execute("INSERT OR REPLACE INTO names VALUES (?,?,?)", (kind, key, name.strip()))
        else:
            self.con.execute("DELETE FROM names WHERE kind=? AND key=?", (kind, key))
        self.con.commit()

    def add_unknown(self, raw: str) -> None:
        self.con.execute("INSERT INTO unknown (ts, raw) VALUES (?,?)", (datetime.now().isoformat(timespec="seconds"), raw))
        self.con.execute("DELETE FROM unknown WHERE id NOT IN (SELECT id FROM unknown ORDER BY id DESC LIMIT 200)")
        self.con.commit()

    def unknown(self) -> list[dict]:
        return [dict(r) for r in self.con.execute("SELECT ts, raw FROM unknown ORDER BY id DESC LIMIT 100")]


# --------------------------------------------------------------------------- Home Assistant
class HaBridge:
    """Pubblica stati ed eventi in Home Assistant tramite l'API del Supervisor."""

    def __init__(self, enabled: bool) -> None:
        self.token = os.environ.get("SUPERVISOR_TOKEN")
        self.enabled = enabled and bool(self.token)
        self.base = "http://supervisor/core/api"
        self.session: ClientSession | None = None
        self._last: dict[str, str] = {}

    async def start(self) -> None:
        if self.enabled:
            self.session = ClientSession(
                headers={"Authorization": f"Bearer {self.token}"}, timeout=ClientTimeout(total=10)
            )

    async def state(self, entity_id: str, state, attrs: dict) -> None:
        if not self.session:
            return
        body = {"state": state, "attributes": attrs}
        key = json.dumps(body, sort_keys=True, default=str)
        if self._last.get(entity_id) == key:
            return
        try:
            async with self.session.post(f"{self.base}/states/{entity_id}", json=body) as r:
                if r.status < 300:
                    self._last[entity_id] = key
                else:
                    _LOGGER.warning("HA %s -> %s", entity_id, r.status)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("HA non raggiungibile: %s", err)

    async def listen(self, event_type: str, handler) -> None:
        """Ascolta un evento di Home Assistant (per comandare l'add-on dalle automazioni)."""
        if not self.session:
            return
        while True:
            try:
                async with self.session.ws_connect("http://supervisor/core/websocket", heartbeat=30) as ws:
                    await ws.receive_json()  # auth_required
                    await ws.send_json({"type": "auth", "access_token": self.token})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        raise RuntimeError("autenticazione websocket fallita")
                    await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": event_type})
                    _LOGGER.info("In ascolto dell'evento %s", event_type)
                    async for msg in ws:
                        if msg.type != WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        if data.get("type") == "event":
                            asyncio.create_task(handler(data["event"].get("data", {})))
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Websocket HA: %s", err)
            await asyncio.sleep(15)

    async def event(self, event_type: str, data: dict) -> None:
        if not self.session:
            return
        try:
            async with self.session.post(f"{self.base}/events/{event_type}", json=data):
                pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("HA evento non inviato: %s", err)


# --------------------------------------------------------------------------- applicazione
class App:
    def __init__(self) -> None:
        self.opt = load_options()
        self.db = Db(DATA / "pbx_d600.db")
        self.client = PbxClient(
            self.opt["host"], self.opt["port"], self.opt["local_port"],
            self.opt["num_extensions"], self.opt["num_trunks"],
        )
        self.ha = HaBridge(self.opt["publish_entities"])
        self.sockets: set[web.WebSocketResponse] = set()
        self._dirty = asyncio.Event()

    # ---- stato
    def snapshot(self) -> dict:
        c = self.client
        ext_names = self.db.names("ext")
        contacts = self.db.names("contact")
        return {
            "version": VERSION,
            "host": self.opt["host"],
            "connected": c.connected,
            "today": self.db.today_counts(),
            "extensions": [
                {
                    "port": port,
                    "number": c.extensions.get(port, str(port)),
                    "name": ext_names.get(c.extensions.get(port, str(port))),
                    "state": c.ext_state[port],
                    "trunk": c.ext_trunk.get(port),
                    "forwarding": self.fwd_view(port),
                }
                for port in sorted(c.ext_state)
            ],
            "trunks": [
                {
                    "trunk": t,
                    "state": c.trunk_state[t],
                    "caller": c.trunk_cid.get(t),
                    "caller_name": contacts.get(c.trunk_cid.get(t) or ""),
                }
                for t in sorted(c.trunk_state)
            ],
            "last_caller": {
                "number": c.last_cid, "name": contacts.get(c.last_cid or ""), "trunk": c.last_cid_trunk,
                "time": c.last_cid_time.isoformat(timespec="seconds") if c.last_cid_time else None,
            },
        }

    def fwd_view(self, port: int) -> dict | None:
        f = self.client.forwarding.get(port)
        if not f:
            return None
        code = f["mode_code"]
        return {
            "code": code,
            "label": FWD_LABELS.get(code, f"Sconosciuta ({code})"),
            "target": f["target"],
            "target_port": f["target_port"],
            "number": f["number"],
        }

    async def broadcast(self, msg: dict) -> None:
        data = json.dumps(msg, default=str)
        for ws in list(self.sockets):
            try:
                await ws.send_str(data)
            except Exception:  # noqa: BLE001
                self.sockets.discard(ws)

    async def publisher(self) -> None:
        """Raggruppa le modifiche e aggiorna plancia ed entità HA."""
        while True:
            await self._dirty.wait()
            await asyncio.sleep(0.3)
            self._dirty.clear()
            snap = self.snapshot()
            await self.broadcast({"type": "state", "data": snap})
            await self.publish_ha(snap)

    async def publish_ha(self, snap: dict) -> None:
        if not self.ha.enabled:
            return
        await self.ha.state(
            "binary_sensor.pbx_d600_collegamento", "on" if snap["connected"] else "off",
            {"friendly_name": "Centralino collegamento", "device_class": "connectivity"},
        )
        for e in snap["extensions"]:
            label = f"Interno {e['number']}" + (f" ({e['name']})" if e["name"] else "")
            await self.ha.state(
                f"sensor.pbx_d600_interno_{e['number']}", e["state"],
                {"friendly_name": label, "icon": "mdi:phone-classic", "porta": e["port"],
                 "numero_interno": e["number"], "nome": e["name"], "linea_esterna": e["trunk"],
                 "deviazione": (e["forwarding"] or {}).get("label"),
                 "deviazione_verso": ((e["forwarding"] or {}).get("target") if (e["forwarding"] or {}).get("code") in (1, 2)
                                      else (e["forwarding"] or {}).get("number") if (e["forwarding"] or {}).get("code") in (5, 6) else None)},
            )
        for t in snap["trunks"]:
            await self.ha.state(
                f"sensor.pbx_d600_linea_{t['trunk']}", t["state"],
                {"friendly_name": f"Linea esterna {t['trunk']}", "icon": "mdi:phone-in-talk",
                 "chiamante": t["caller"], "nome_chiamante": t["caller_name"]},
            )
        lc = snap["last_caller"]
        await self.ha.state(
            "sensor.pbx_d600_ultimo_chiamante", lc["number"] or "nessuno",
            {"friendly_name": "Ultimo chiamante", "icon": "mdi:card-account-phone",
             "nome": lc["name"], "linea": lc["trunk"], "ora": lc["time"]},
        )
        for key, label, icon in (("risposta", "Chiamate ricevute oggi", "mdi:phone-incoming"),
                                 ("persa", "Chiamate perse oggi", "mdi:phone-missed"),
                                 ("effettuata", "Chiamate effettuate oggi", "mdi:phone-outgoing")):
            await self.ha.state(
                f"sensor.pbx_d600_{key}_oggi", snap["today"][key],
                {"friendly_name": label, "icon": icon, "unit_of_measurement": "chiamate", "state_class": "total"},
            )

    # ---- eventi dal centralino
    def on_message(self, msg: p.Message) -> None:
        if msg.kind == "unknown":
            self.db.add_unknown(msg.raw.hex(" "))
            _LOGGER.warning("Messaggio non riconosciuto: %s", msg.raw.hex(" "))
        if msg.kind not in ("clock_ack", "memory"):
            self._dirty.set()

    def on_call_ended(self, call: dict) -> None:
        self.db.add_call(call)
        contacts = self.db.names("contact")
        data = {**call, "nome": contacts.get(call.get("numero") or "")}
        asyncio.get_running_loop().create_task(self.broadcast({"type": "call", "data": data}))
        asyncio.get_running_loop().create_task(self.ha.event("pbx_d600_call", data))
        self._dirty.set()

    async def keep_numbers(self) -> None:
        """Legge i numeri interni dal centralino (riprova finché risponde)."""
        while not await self.client.load_extension_numbers():
            await asyncio.sleep(30)
        await self.refresh_forwarding()
        while True:  # rilettura periodica: le deviazioni si possono cambiare anche dai telefoni (#81/#82)
            await asyncio.sleep(300)
            await self.refresh_forwarding()

    async def refresh_forwarding(self) -> None:
        if await self.client.read_forwarding() is None:
            _LOGGER.warning("Impossibile leggere le deviazioni dal centralino")
        self._dirty.set()

    async def ha_set_forwarding(self, data: dict) -> None:
        try:
            await self.apply_forwarding(data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Deviazione da Home Assistant non applicata: %s", err)
            await self.ha.event("pbx_d600_errore", {"errore": str(err), "richiesta": data})

    async def apply_forwarding(self, data: dict) -> dict:
        """Imposta la deviazione. data: interno (numero) o porta, modalita (codice o chiave), destinazione/numero."""
        c = self.client
        ext = str(data.get("interno") or data.get("extension") or "").strip()
        port = int(data["porta"]) if data.get("porta") else next((p_ for p_, n in c.extensions.items() if n == ext), None)
        if not port:
            raise ValueError(f"Interno {ext} non trovato")
        mode = data.get("modalita", data.get("mode"))
        code = FWD_KEYS[mode] if isinstance(mode, str) and mode in FWD_KEYS else int(mode)
        target_port = None
        number = None
        if code in (1, 2):
            dest = str(data.get("destinazione") or data.get("target") or "").strip()
            target_port = next((p_ for p_, n in c.extensions.items() if n == dest), None)
            if not target_port:
                raise ValueError(f"Interno di destinazione {dest} non trovato")
        if code in (5, 6):
            number = "".join(ch for ch in str(data.get("numero") or data.get("number") or "") if ch.isdigit())
            if not number:
                raise ValueError("Numero esterno mancante")
        ok = await c.set_forwarding(port, code, target_port=target_port, number=number)
        if not ok:
            raise RuntimeError("Il centralino non ha confermato la scrittura")
        _LOGGER.info("Deviazione interno %s: %s %s", c.extensions.get(port), FWD_LABELS[code], number or (c.extensions.get(target_port) if target_port else ""))
        self._dirty.set()
        await self.ha.event("pbx_d600_deviazione", {"interno": c.extensions.get(port), "modalita": FWD_LABELS[code],
                                                    "destinazione": c.extensions.get(target_port) if target_port else None, "numero": number})
        return {"ok": True, "forwarding": self.fwd_view(port)}

    # ---- web
    @web.middleware
    async def only_local(self, request: web.Request, handler):
        # la plancia è servita solo tramite Ingress di HA (e localhost): con host_network
        # la porta sarebbe altrimenti raggiungibile da tutta la rete senza autenticazione
        if request.remote not in ("172.30.32.2", "127.0.0.1", "::1"):
            raise web.HTTPForbidden(text="Accesso consentito solo tramite Home Assistant")
        return await handler(request)

    def routes(self) -> web.Application:
        app = web.Application(middlewares=[self.only_local])
        app.add_routes([
            web.get("/", self.h_index),
            web.get("/api/state", self.h_state),
            web.get("/api/calls", self.h_calls),
            web.get("/api/calls.csv", self.h_csv),
            web.get("/api/names", self.h_names),
            web.post("/api/names", self.h_set_name),
            web.get("/api/diagnostics", self.h_diag),
            web.post("/api/clock", self.h_clock),
            web.get("/api/forwarding", self.h_fwd_get),
            web.post("/api/forwarding", self.h_fwd_set),
            web.get("/ws", self.h_ws),
        ])
        return app

    async def h_index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    async def h_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.snapshot(), dumps=lambda o: json.dumps(o, default=str))

    def _query(self, request: web.Request) -> tuple:
        q = request.query
        return (q.get("esito") or None, q.get("q") or None, q.get("from") or None, q.get("to") or None)

    async def h_calls(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 50)), 500)
        offset = int(request.query.get("offset", 0))
        rows, total = self.db.calls(*self._query(request), limit, offset)
        contacts = self.db.names("contact")
        ext_names = self.db.names("ext")
        for r in rows:
            r["nome"] = contacts.get(r["numero"] or "")
            r["nome_interno"] = ext_names.get(r["interno"] or "")
        return web.json_response({"total": total, "calls": rows})

    async def h_csv(self, request: web.Request) -> web.Response:
        rows, _ = self.db.calls(*self._query(request), 100000, 0)
        contacts = self.db.names("contact")
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Inizio", "Direzione", "Esito", "Numero", "Nome", "Interno", "Linea", "Durata (s)"])
        for r in rows:
            w.writerow([r["inizio"], r["direzione"], r["esito"], r["numero"] or "", contacts.get(r["numero"] or "", ""),
                        r["interno"] or "", r["linea"], r["durata"]])
        return web.Response(
            body=("﻿" + buf.getvalue()).encode(), content_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=registro_chiamate.csv"},
        )

    async def h_names(self, request: web.Request) -> web.Response:
        return web.json_response({"ext": self.db.names("ext"), "contact": self.db.names("contact")})

    async def h_set_name(self, request: web.Request) -> web.Response:
        body = await request.json()
        kind = body.get("kind")
        if kind not in ("ext", "contact") or not str(body.get("key", "")).strip():
            raise web.HTTPBadRequest(text="kind/key non validi")
        self.db.set_name(kind, str(body["key"]).strip(), str(body.get("name", "")))
        self._dirty.set()
        return web.json_response({"ok": True})

    async def h_diag(self, request: web.Request) -> web.Response:
        return web.json_response({
            "connected": self.client.connected, "host": self.opt["host"], "port": self.opt["port"],
            "local_port": self.opt["local_port"], "ha_entities": self.ha.enabled, "version": VERSION,
            "unknown": self.db.unknown(),
        })

    async def h_fwd_get(self, request: web.Request) -> web.Response:
        if request.query.get("refresh"):
            await self.client.read_forwarding()
        return web.json_response({
            "modes": [{"code": k, "label": v} for k, v in FWD_LABELS.items()],
            "extensions": [
                {"port": port, "number": self.client.extensions.get(port, str(port)),
                 "name": self.db.names("ext").get(self.client.extensions.get(port, str(port))),
                 "forwarding": self.fwd_view(port)}
                for port in sorted(self.client.ext_state)
            ],
        })

    async def h_fwd_set(self, request: web.Request) -> web.Response:
        try:
            return web.json_response(await self.apply_forwarding(await request.json()))
        except (ValueError, KeyError) as err:
            return web.json_response({"ok": False, "error": str(err)}, status=400)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Errore impostando la deviazione")
            return web.json_response({"ok": False, "error": str(err)}, status=500)

    async def h_clock(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": await self.client.sync_clock()})

    async def h_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.sockets.add(ws)
        await ws.send_str(json.dumps({"type": "state", "data": self.snapshot()}, default=str))
        try:
            async for m in ws:
                if m.type == WSMsgType.ERROR:
                    break
        finally:
            self.sockets.discard(ws)
        return ws

    # ---- avvio
    async def run(self) -> None:
        if not self.opt["host"]:
            _LOGGER.error("Imposta l'indirizzo IP del centralino nella configurazione dell'add-on")
        self.client.add_listener(self.on_message)
        self.client.on_call_ended = self.on_call_ended
        await self.ha.start()

        runner = web.AppRunner(self.routes())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        _LOGGER.info("Plancia in ascolto sulla porta %s", PORT)

        if self.opt["host"]:
            while True:
                try:
                    await self.client.start()
                    break
                except OSError as err:
                    _LOGGER.error("Porta UDP %s non disponibile (%s): l'integrazione PBX D600 è ancora attiva? Riprovo tra 30 s",
                                  self.opt["local_port"], err)
                    await asyncio.sleep(30)
            _LOGGER.info("Collegamento al centralino %s:%s", self.opt["host"], self.opt["port"])
            asyncio.create_task(self.keep_numbers())

        asyncio.create_task(self.publisher())
        asyncio.create_task(self.ha.listen("pbx_d600_imposta_deviazione", self.ha_set_forwarding))
        self._dirty.set()
        while True:  # aggiornamento periodico (stato collegamento, contatori del giorno)
            await asyncio.sleep(30)
            self._dirty.set()


if __name__ == "__main__":
    asyncio.run(App().run())
