# PBX D600 per Home Assistant

Integrazione per il centralino gestito dal programma "PBX D600" (modello D832-L: 8 linee esterne, 32 interni). Home Assistant comunica **direttamente col centralino via UDP**, senza il PC.

## Cosa fa (versione 0.1)

- **Stato di ogni interno**, con il numero letto dal centralino (41, 42, 101, …): `libero`, `sganciato`, `squilla`, `in_linea_esterna`.
- **Stato di ogni linea esterna**: `libera`, `squilla`, `occupata`, con il numero del chiamante.
- **Sensore "Ultimo chiamante"**: numero, linea e ora.
- **Sensore "Collegamento"**: indica se il centralino risponde.
- **Sincronizzazione dell'ora**: ogni 30 secondi, come fa il programma originale.
- **Evento `pbx_d600_event`** per ogni messaggio, da usare nelle automazioni.
- **Servizi**:
  - `pbx_d600.sync_clock`
  - `pbx_d600.read_memory`: lettura della configurazione, innocua.
  - `pbx_d600.send_raw`: avanzato, da usare con cautela.

## Installazione

1. Copia la cartella `custom_components/pbx_d600` in `/config/custom_components/`. Puoi usare l'add-on "File editor" o "Samba share".
2. Riavvia Home Assistant.
3. **Chiudi il programma PBX D600 sul PC.** Il centralino invia gli eventi a un solo client.
4. Vai in Impostazioni → Dispositivi e servizi → Aggiungi integrazione → "PBX D600".
5. Inserisci l'IP del centralino (es. 192.168.x.x), la porta 50001, la porta locale 50000, 32 interni e 8 linee.

## Registro chiamate (dalla 0.2)

- **Sensori**: "Chiamate ricevute", "Chiamate perse" e "Chiamate effettuate".
  - Lo stato è il numero di chiamate di oggi.
  - L'attributo `chiamate` contiene le ultime 30, dalla più recente.
  - Ogni chiamata ha questi campi: direzione, linea, numero, interno, inizio, risposta, fine, durata (secondi), esito.
- **Persistenza**: il registro viene salvato e sopravvive ai riavvii (ultime 200 chiamate).
- **Evento `pbx_d600_call`**: viene emesso a fine chiamata, con tutti i dati.
- **Chiamate in uscita**: vengono riconosciute quando un interno impegna una linea esterna. Il numero composto non è ancora decifrato. I messaggi sconosciuti finiscono nel sensore diagnostico "Messaggi non riconosciuti" e servono per aggiungerlo.

### Scheda per la dashboard (Markdown)

```yaml
type: markdown
title: Chiamate
content: >-
  {% set ric = state_attr('sensor.centralino_pbx_d600_chiamate_ricevute','chiamate') or [] %}
  {% set per = state_attr('sensor.centralino_pbx_d600_chiamate_perse','chiamate') or [] %}
  {% set eff = state_attr('sensor.centralino_pbx_d600_chiamate_effettuate','chiamate') or [] %}
  {% set tutte = (ric + per + eff) | sort(attribute='inizio', reverse=true) %}
  | | Ora | Numero | Interno | Linea | Durata |
  |---|---|---|---|---|---|
  {% for c in tutte[:20] -%}
  | {{ {'risposta':'📥','persa':'❌','effettuata':'📤','non risposta':'📤❌'}[c.esito] }} | {{ as_timestamp(c.inizio) | timestamp_custom('%d/%m %H:%M') }} | {{ c.numero or '-' }} | {{ c.interno or '-' }} | {{ c.linea }} | {{ (c.durata // 60) }}:{{ '%02d' % (c.durata % 60) }} |
  {% endfor %}
```

## Esempio di automazione: notifica di chiamata in arrivo

```yaml
alias: Notifica chiamata in arrivo
triggers:
  - trigger: event
    event_type: pbx_d600_event
    event_data:
      type: caller_id
actions:
  - action: notify.notify
    data:
      title: "Chiamata in arrivo"
      message: "Linea {{ trigger.event.data.trunk }}: {{ trigger.event.data.number }}"
```

Tipi di evento: `off_hook`, `on_hook`, `ext_ringing`, `trunk_ringing`, `trunk_released`, `trunk_connected`, `caller_id`, `unknown`.

I messaggi non ancora decifrati arrivano come `unknown`, con il campo `raw`. Vengono scritti anche nel log, così si possono aggiungere in seguito.

## Protocollo (ricostruito)

UDP: Home Assistant usa la porta 50000, il centralino la 50001. Ogni frame ha questa forma:

`[LUNGHEZZA] [COMANDO] [dati…] [XOR di tutti i byte precedenti]`

| Direzione | Comando | Significato |
|---|---|---|
| HA→PBX | `03 h h m m s s` | imposta l'ora (heartbeat ogni 30 s); risposta `05 03 00 00 06` |
| HA→PBX | `04 01 AH AL 80` | legge 128 byte di memoria; la risposta riporta lo stesso header + i dati |
| PBX→HA | `10 P` / `11 P` | interno (porta P) sganciato / agganciato |
| PBX→HA | `14 P` | interno P squilla |
| PBX→HA | `17 T` / `18 T` | linea T squilla / si libera |
| PBX→HA | `80 T P` | linea T collegata all'interno P |
| PBX→HA | `08 T 01 N cifre` | caller ID (cifre BCD con nibble invertiti, 0xD = fine); HA risponde `08 T` |

Memoria: la tabella dei numeri interni parte da `0x0300`, con 4 byte per porta.
