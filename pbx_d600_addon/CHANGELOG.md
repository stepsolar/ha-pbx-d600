## 1.1.1
- Plancia ottimizzata per smartphone: intestazione compatta con menu scorrevole, tabelle di Registro, Interni, Deviazioni e Rubrica trasformate in schede leggibili, campi senza zoom automatico su iPhone.

## 1.1.0
- **Deviazione di chiamata per interno** dalla nuova scheda *Impostazioni*: non trasferire, inoltra all'interno, all'interno se occupato, alla linea esterna, linea esterna se occupato (con numero di telefono). Scrittura diretta nel centralino, stessi comandi del programma originale.
- Deviazioni attive mostrate negli Interni e negli attributi delle entità `sensor.pbx_d600_interno_*`; rilettura automatica ogni 5 minuti (anche quelle impostate dai telefoni con #81/#82).
- Comando da Home Assistant con l'evento `pbx_d600_imposta_deviazione`; conferma con l'evento `pbx_d600_deviazione`.
- Corretto l'ack del riepilogo di fine chiamata (`04 01 <indice>`): il centralino non lo ripete più e i riepiloghi arretrati vengono registrati.
- Logo e icona dell'add-on.

## 1.0.1
- Corretta la build con Supervisor 2026.04+ (immagine base esplicita nel Dockerfile, non più BUILD_FROM).

## 1.0.0
- Prima versione dell'add-on: collegamento UDP diretto, plancia in barra laterale, registro chiamate su SQLite, rubrica e nomi interni, entità ed eventi Home Assistant.
