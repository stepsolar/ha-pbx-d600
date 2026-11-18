"""Costanti per l'integrazione PBX D600."""
DOMAIN = "pbx_d600"

CONF_LOCAL_PORT = "local_port"
CONF_NUM_EXT = "num_extensions"
CONF_NUM_TRUNKS = "num_trunks"

DEFAULT_PORT = 50001
DEFAULT_LOCAL_PORT = 50000
DEFAULT_NUM_EXT = 32
DEFAULT_NUM_TRUNKS = 8

HEARTBEAT_INTERVAL = 30  # secondi, come il software originale
OFFLINE_AFTER = 95  # secondi senza risposta -> disconnesso

SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"
EVENT_PBX = f"{DOMAIN}_event"

# Stati interni
EXT_IDLE = "libero"
EXT_OFFHOOK = "sganciato"
EXT_RINGING = "squilla"
EXT_EXTERNAL = "in_linea_esterna"

# Stati linee esterne
TRUNK_IDLE = "libera"
TRUNK_RINGING = "squilla"
TRUNK_BUSY = "occupata"
