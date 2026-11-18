# PBX D600 – add-on

L'add-on collega Home Assistant direttamente al centralino PBX D600 (UDP, porte 50000/50001), senza il PC. Aggiunge alla barra laterale la plancia **Centralino**.

## Plancia

- **Panoramica**:
  - contatori di oggi;
  - linee esterne in tempo reale, con il nome o il numero di chi chiama;
  - griglia degli interni;
  - ultime chiamate.
- **Registro chiamate**:
  - filtri Ricevute / Perse / Effettuate;
  - ricerca per numero, nome o interno;
  - intervallo di date;
  - esportazione CSV.
- **Interni**: nome di ogni interno e deviazione attiva.
- **Impostazioni**: deviazione di chiamata per interno, senza alzare la cornetta.
- **Rubrica**: associa un nome ai numeri, che verrà mostrato nel registro e nelle notifiche.
- **Diagnostica**: stato del collegamento, sincronizzazione dell'ora e messaggi del centralino non ancora decifrati.

## Deviazione di chiamata

Nella scheda **Impostazioni** scegli:
1. l'interno;
2. il tipo di trasferimento;
3. la destinazione: un interno, oppure un numero di telefono per i trasferimenti verso linea esterna.

Poi premi **Applica**. È lo stesso risultato dei codici `#81…`/`#82…` composti dal telefono, ma la scrittura avviene direttamente nella memoria del centralino.

| Trasferimento | Codice telefono equivalente |
|---|---|
| Non trasferire | `#81#` / `#82#` |
| Inoltra all'interno | `#81 ABCD` |
| Trasferimento all'interno se occupato | `#82 ABCD` |
| Trasferimento alla linea esterna | `#81*numero#` |
| Trasferimento linea esterna se occupato | `#82*numero#` |

Dalle automazioni puoi usare l'evento `pbx_d600_imposta_deviazione`. Il campo `modalita` accetta `nessuna`, `interno`, `interno_occupato`, `esterno` oppure `esterno_occupato`:

```yaml
actions:
  - event: pbx_d600_imposta_deviazione
    event_data:
      interno: "42"
      modalita: esterno_occupato
      numero: "3201936661"
```

Per una deviazione verso un interno usa `destinazione: "101"` al posto di `numero`. Ad applicazione avvenuta l'add-on emette l'evento `pbx_d600_deviazione`; in caso di errore emette `pbx_d600_errore`.

## Configurazione

| Opzione | Descrizione |
|---|---|
| `host` | IP del centralino (es. `192.168.2.239`) |
| `port` | porta UDP del centralino (50001) |
| `local_port` | porta UDP locale su cui arrivano gli eventi (50000) |
| `num_extensions` / `num_trunks` | numero di interni e di linee esterne (32 / 8) |
| `publish_entities` | crea le entità in Home Assistant ed emette l'evento `pbx_d600_call` |

**Importante:** il centralino parla con un solo client. Prima di avviare l'add-on:
- rimuovi l'integrazione *PBX D600* (Impostazioni → Dispositivi e servizi), che userebbe la stessa porta 50000;
- chiudi il programma PBX D600 sul PC.

## Entità create in Home Assistant

- `binary_sensor.pbx_d600_collegamento`
- `sensor.pbx_d600_interno_<numero>`: stato `libero` / `sganciato` / `squilla` / `in_linea_esterna`
- `sensor.pbx_d600_linea_<n>`: stato `libera` / `squilla` / `occupata`; attributi `chiamante` e `nome_chiamante`
- `sensor.pbx_d600_ultimo_chiamante`
- `sensor.pbx_d600_risposta_oggi`, `sensor.pbx_d600_persa_oggi`, `sensor.pbx_d600_effettuata_oggi`

A ogni chiamata conclusa viene emesso l'evento `pbx_d600_call`, con i campi direzione, linea, numero, nome, interno, inizio, durata ed esito.

Esempio di notifica per una chiamata persa:

```yaml
triggers:
  - trigger: event
    event_type: pbx_d600_call
    event_data:
      esito: persa
actions:
  - action: notify.notify
    data:
      title: Chiamata persa
      message: "{{ trigger.event.data.nome or trigger.event.data.numero or 'Numero sconosciuto' }} (linea {{ trigger.event.data.linea }})"
```

Il registro è salvato in `/data/pbx_d600.db` (SQLite) ed è incluso nei backup dell'add-on.
