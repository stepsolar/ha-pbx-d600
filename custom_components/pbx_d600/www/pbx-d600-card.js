/*
 * PBX D600 Card - scheda Lovelace per l'integrazione pbx_d600.
 * Si registra da sola quando l'integrazione è caricata: nessuna risorsa da aggiungere.
 *
 *   type: custom:pbx-d600-card
 *   title: Centralino            (opzionale)
 *   show_extensions: true        (opzionale, mostra la griglia interni)
 *   show_trunks: true            (opzionale, mostra le linee esterne)
 *   max_calls: 25                (opzionale, righe nel registro)
 */
const VERSION = "0.4.0";

const EXT = {
  libero: { label: "Libero", color: "var(--disabled-text-color, #9e9e9e)", icon: "mdi:phone-hangup" },
  sganciato: { label: "Sganciato", color: "var(--warning-color, #ff9800)", icon: "mdi:phone-dial" },
  squilla: { label: "Squilla", color: "var(--info-color, #039be5)", icon: "mdi:phone-ring" },
  in_linea_esterna: { label: "In linea", color: "var(--success-color, #43a047)", icon: "mdi:phone-in-talk" },
};
const TRUNK = {
  libera: { label: "Libera", color: "var(--disabled-text-color, #9e9e9e)", icon: "mdi:phone-hangup" },
  squilla: { label: "Squilla", color: "var(--info-color, #039be5)", icon: "mdi:phone-ring" },
  occupata: { label: "In uso", color: "var(--success-color, #43a047)", icon: "mdi:phone-in-talk" },
};
const CALL = {
  risposta: { label: "Ricevuta", color: "var(--success-color, #43a047)", icon: "mdi:phone-incoming" },
  persa: { label: "Persa", color: "var(--error-color, #db4437)", icon: "mdi:phone-missed" },
  effettuata: { label: "Effettuata", color: "var(--info-color, #039be5)", icon: "mdi:phone-outgoing" },
  "non risposta": { label: "Non risposta", color: "var(--warning-color, #ff9800)", icon: "mdi:phone-cancel" },
};
const TABS = [
  ["tutte", "Tutte"],
  ["risposta", "Ricevute"],
  ["persa", "Perse"],
  ["effettuata", "Effettuate"],
];

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function fmtDuration(sec) {
  sec = Number(sec) || 0;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtWhen(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const now = new Date();
  const time = d.toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  if (sameDay(d, now)) return `Oggi ${time}`;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (sameDay(d, y)) return `Ieri ${time}`;
  return `${d.toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit" })} ${time}`;
}

class PbxD600Card extends HTMLElement {
  constructor() {
    super();
    this._tab = "tutte";
    this._sig = "";
    this.attachShadow({ mode: "open" });
  }

  static getStubConfig() {
    return { show_extensions: true, show_trunks: true, max_calls: 25 };
  }

  setConfig(config) {
    this._config = { show_extensions: true, show_trunks: true, max_calls: 25, ...config };
    this._sig = "";
    if (this._hass) this._render();
  }

  getCardSize() {
    return 10;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    const ents = this._entities();
    // ridisegna solo se cambiano le entità del centralino
    const sig = ents.all.map((e) => `${e.entity_id}|${e.state}|${e.last_updated}`).join(";");
    if (sig !== this._sig) {
      this._sig = sig;
      this._render(ents);
    }
  }

  _entities() {
    const hass = this._hass;
    const reg = hass.entities || {};
    const all = Object.keys(hass.states)
      .filter((id) => reg[id] ? reg[id].platform === "pbx_d600" : false)
      .map((id) => hass.states[id]);
    const out = { all, ext: [], trunks: [], logs: [], conn: null, last: null };
    for (const s of all) {
      const a = s.attributes;
      if (s.entity_id.startsWith("binary_sensor.")) out.conn = s;
      else if (a.porta !== undefined) out.ext.push(s);
      else if (a.chiamate !== undefined) out.logs.push(s);
      else if (a.chiamante !== undefined) out.trunks.push(s);
      else if (a.linea !== undefined && a.ora !== undefined) out.last = s;
    }
    out.ext.sort((x, y) => x.attributes.porta - y.attributes.porta);
    out.trunks.sort((x, y) => (x.attributes.friendly_name || "").localeCompare(y.attributes.friendly_name || "", "it", { numeric: true }));
    return out;
  }

  _calls(logs) {
    const seen = new Set();
    const calls = [];
    for (const l of logs) {
      for (const c of l.attributes.chiamate || []) {
        const key = `${c.inizio}|${c.linea}|${c.direzione}`;
        if (!seen.has(key)) {
          seen.add(key);
          calls.push(c);
        }
      }
    }
    calls.sort((a, b) => (b.inizio || "").localeCompare(a.inizio || ""));
    return calls;
  }

  _count(logs, outcome) {
    const today = new Date().toDateString();
    return this._calls(logs).filter((c) => c.esito === outcome && new Date(c.inizio).toDateString() === today).length;
  }

  _render(ents) {
    if (!this._config || !this._hass) return;
    ents = ents || this._entities();
    const cfg = this._config;
    const title = cfg.title || "Centralino PBX D600";

    if (!ents.all.length) {
      this.shadowRoot.innerHTML = `<ha-card header="${esc(title)}"><div class="empty">Nessuna entità dell'integrazione PBX D600 trovata.</div></ha-card>`;
      return;
    }

    const online = ents.conn && ents.conn.state === "on";
    const calls = this._calls(ents.logs);
    const shown = (this._tab === "tutte" ? calls : calls.filter((c) => c.esito === this._tab || (this._tab === "effettuata" && c.esito === "non risposta"))).slice(0, cfg.max_calls);

    const active = ents.ext.filter((e) => e.state !== "libero").length;
    const busyTrunks = ents.trunks.filter((t) => t.state !== "libera").length;

    const trunksHtml = ents.trunks
      .map((t, i) => {
        const st = TRUNK[t.state] || TRUNK.libera;
        const caller = t.attributes.chiamante;
        return `<div class="trunk ${t.state === "squilla" ? "ring" : ""}" style="--c:${st.color}" data-entity="${t.entity_id}">
            <ha-icon icon="${st.icon}"></ha-icon>
            <div class="tname">Linea ${i + 1}</div>
            <div class="tstate">${esc(caller || st.label)}</div>
          </div>`;
      })
      .join("");

    const extHtml = ents.ext
      .map((e) => {
        const st = EXT[e.state] || EXT.libero;
        const num = e.attributes.numero_interno || e.attributes.porta;
        return `<div class="ext ${e.state === "squilla" ? "ring" : ""} ${e.state === "libero" ? "idle" : ""}" style="--c:${st.color}" title="${esc(st.label)}" data-entity="${e.entity_id}">
            <ha-icon icon="${st.icon}"></ha-icon><span>${esc(num)}</span>
          </div>`;
      })
      .join("");

    const callsHtml = shown.length
      ? shown
          .map((c) => {
            const st = CALL[c.esito] || CALL.effettuata;
            const sub = [c.interno ? `Int. ${esc(c.interno)}` : null, c.linea ? `Linea ${esc(c.linea)}` : null, st.label].filter(Boolean).join(" · ");
            const num = c.numero ? `<a href="tel:${esc(c.numero)}">${esc(c.numero)}</a>` : `<span class="unk">Numero sconosciuto</span>`;
            return `<div class="call">
                <div class="cicon" style="--c:${st.color}"><ha-icon icon="${st.icon}"></ha-icon></div>
                <div class="cmain"><div class="cnum">${num}</div><div class="csub">${sub}</div></div>
                <div class="cright"><div class="cwhen">${esc(fmtWhen(c.inizio))}</div><div class="cdur">${c.durata ? fmtDuration(c.durata) : "—"}</div></div>
              </div>`;
          })
          .join("")
      : `<div class="empty">Nessuna chiamata${this._tab === "tutte" ? "" : " in questa categoria"}.</div>`;

    this.shadowRoot.innerHTML = `
      <style>${PbxD600Card.styles}</style>
      <ha-card>
        <div class="head">
          <div class="title"><ha-icon icon="mdi:phone-classic"></ha-icon>${esc(title)}</div>
          <div class="pill ${online ? "on" : "off"}"><span class="dot"></span>${online ? "Collegato" : "Non collegato"}</div>
        </div>

        <div class="stats">
          <div class="stat" style="--c:${CALL.risposta.color}"><ha-icon icon="mdi:phone-incoming"></ha-icon><b>${this._count(ents.logs, "risposta")}</b><span>Ricevute oggi</span></div>
          <div class="stat" style="--c:${CALL.persa.color}"><ha-icon icon="mdi:phone-missed"></ha-icon><b>${this._count(ents.logs, "persa")}</b><span>Perse oggi</span></div>
          <div class="stat" style="--c:${CALL.effettuata.color}"><ha-icon icon="mdi:phone-outgoing"></ha-icon><b>${this._count(ents.logs, "effettuata")}</b><span>Effettuate oggi</span></div>
          <div class="stat" style="--c:var(--primary-color)"><ha-icon icon="mdi:phone-in-talk"></ha-icon><b>${busyTrunks}</b><span>Linee in uso</span></div>
        </div>

        ${cfg.show_trunks && ents.trunks.length ? `<div class="section"><div class="stitle">Linee esterne</div><div class="trunks">${trunksHtml}</div></div>` : ""}

        ${cfg.show_extensions && ents.ext.length ? `<div class="section"><div class="stitle">Interni <span class="muted">${active} attivi su ${ents.ext.length}</span></div><div class="exts">${extHtml}</div></div>` : ""}

        <div class="section">
          <div class="stitle">Registro chiamate</div>
          <div class="tabs">${TABS.map(([k, l]) => `<button class="tab ${this._tab === k ? "sel" : ""}" data-tab="${k}">${l}</button>`).join("")}</div>
          <div class="calls">${callsHtml}</div>
        </div>
      </ha-card>`;

    this.shadowRoot.querySelectorAll(".tab").forEach((b) =>
      b.addEventListener("click", () => {
        this._tab = b.dataset.tab;
        this._render();
      })
    );
    this.shadowRoot.querySelectorAll("[data-entity]").forEach((el) =>
      el.addEventListener("click", () => {
        const ev = new Event("hass-more-info", { bubbles: true, composed: true });
        ev.detail = { entityId: el.dataset.entity };
        this.dispatchEvent(ev);
      })
    );
  }

  static get styles() {
    return `
      ha-card { padding: 16px; }
      .head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:14px; }
      .title { display:flex; align-items:center; gap:10px; font-size:1.25rem; font-weight:500; }
      .title ha-icon { color: var(--primary-color); }
      .pill { display:flex; align-items:center; gap:6px; font-size:.8rem; padding:4px 10px; border-radius:999px; font-weight:500; }
      .pill .dot { width:8px; height:8px; border-radius:50%; background:currentColor; }
      .pill.on { color: var(--success-color, #43a047); background: color-mix(in srgb, var(--success-color, #43a047) 14%, transparent); }
      .pill.off { color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 14%, transparent); }

      .stats { display:grid; grid-template-columns: repeat(4, 1fr); gap:8px; margin-bottom:18px; }
      .stat { display:grid; grid-template-columns:auto 1fr; grid-template-rows:auto auto; column-gap:8px; align-items:center;
              padding:10px 12px; border-radius:12px; background: color-mix(in srgb, var(--c) 10%, transparent); }
      .stat ha-icon { grid-row: span 2; color: var(--c); --mdc-icon-size: 26px; }
      .stat b { font-size:1.4rem; line-height:1.1; }
      .stat span { font-size:.72rem; color: var(--secondary-text-color); }

      .section { margin-top:14px; }
      .stitle { font-size:.8rem; text-transform:uppercase; letter-spacing:.06em; color: var(--secondary-text-color); margin-bottom:8px; font-weight:500; }
      .muted { text-transform:none; letter-spacing:0; opacity:.8; margin-left:6px; }

      .trunks { display:grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap:8px; }
      .trunk { cursor:pointer; padding:10px; border-radius:12px; border:1px solid color-mix(in srgb, var(--c) 45%, transparent);
               background: color-mix(in srgb, var(--c) 9%, transparent); }
      .trunk ha-icon { color: var(--c); }
      .tname { font-weight:500; margin-top:4px; }
      .tstate { font-size:.8rem; color: var(--secondary-text-color); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

      .exts { display:grid; grid-template-columns: repeat(auto-fill, minmax(70px, 1fr)); gap:6px; }
      .ext { cursor:pointer; display:flex; align-items:center; gap:6px; padding:6px 8px; border-radius:10px; font-weight:500;
             background: color-mix(in srgb, var(--c) 14%, transparent); }
      .ext ha-icon { color: var(--c); --mdc-icon-size: 18px; }
      .ext.idle { background: var(--secondary-background-color, rgba(127,127,127,.08)); font-weight:400; color: var(--secondary-text-color); }

      .ring { animation: ring 1s ease-in-out infinite; }
      @keyframes ring { 0%,100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--c) 50%, transparent); } 50% { box-shadow: 0 0 0 5px transparent; } }

      .tabs { display:flex; gap:6px; margin-bottom:8px; flex-wrap:wrap; }
      .tab { border:none; cursor:pointer; font: inherit; font-size:.85rem; padding:6px 12px; border-radius:999px;
             background: var(--secondary-background-color, rgba(127,127,127,.1)); color: var(--primary-text-color); }
      .tab.sel { background: var(--primary-color); color: var(--text-primary-color, #fff); }

      .calls { display:flex; flex-direction:column; }
      .call { display:flex; align-items:center; gap:12px; padding:10px 4px; border-bottom:1px solid var(--divider-color); }
      .call:last-child { border-bottom:none; }
      .cicon { width:36px; height:36px; border-radius:50%; display:flex; align-items:center; justify-content:center; flex:none;
               background: color-mix(in srgb, var(--c) 15%, transparent); }
      .cicon ha-icon { color: var(--c); --mdc-icon-size: 20px; }
      .cmain { flex:1; min-width:0; }
      .cnum { font-weight:500; font-size:1rem; }
      .cnum a { color: inherit; text-decoration:none; }
      .unk { color: var(--secondary-text-color); font-weight:400; font-style:italic; }
      .csub { font-size:.8rem; color: var(--secondary-text-color); }
      .cright { text-align:right; flex:none; }
      .cwhen { font-size:.85rem; }
      .cdur { font-size:.8rem; color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
      .empty { padding:16px 4px; color: var(--secondary-text-color); text-align:center; }

      @media (max-width: 520px) { .stats { grid-template-columns: repeat(2, 1fr); } }
    `;
  }
}

if (!customElements.get("pbx-d600-card")) {
  customElements.define("pbx-d600-card", PbxD600Card);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "pbx-d600-card",
    name: "Centralino PBX D600",
    description: "Stato di linee e interni e registro delle chiamate ricevute, perse ed effettuate.",
    preview: true,
  });
  console.info(`%c PBX-D600-CARD %c ${VERSION} `, "background:#039be5;color:#fff", "");
}
