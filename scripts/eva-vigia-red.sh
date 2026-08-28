#!/usr/bin/env bash
# Vigila el trafico de red y avisa SOLO cuando cambia de forma que importa.
# Un vigia que avisa cada dia se ignora al tercero.
set -euo pipefail

IFAZ=${EVA_IFAZ:-eth0}
ESTADO=${EVA_VIGIA_ESTADO:-/var/lib/evastate/vigia-red.json}
VENTANA=${EVA_VIGIA_SEG:-120}
TOKEN_F=${EVA_VIGIA_TOKEN:-}
BASE=${EVA_VIGIA_URL:-}

leer() { cat "/sys/class/net/$IFAZ/statistics/$1"; }

rx1=$(leer rx_packets); tx1=$(leer tx_bytes); by1=$(leer rx_bytes)
sleep "$VENTANA"
rx2=$(leer rx_packets); tx2=$(leer tx_bytes); by2=$(leer rx_bytes)

ppm=$(( (rx2 - rx1) * 60 / VENTANA ))
kb_in=$(( (by2 - by1) * 60 / VENTANA / 1024 ))
kb_out=$(( (tx2 - tx1) * 60 / VENTANA / 1024 ))

# Referencia: la mediana de lo visto hasta ahora, no un umbral inventado.
python3 - "$ESTADO" "$ppm" "$kb_in" "$kb_out" <<'PY' > /tmp/vigia-salida.txt
import json, os, statistics, sys
est, ppm, kin, kout = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
h = {"muestras": []}
if os.path.exists(est):
    try: h = json.load(open(est))
    except Exception: pass
m = h.get("muestras", [])
motivos = []
if len(m) >= 5:
    base = statistics.median(m)
    if base > 0 and ppm > base * 10:
        motivos.append(f"paquetes entrantes {ppm}/min, {ppm/base:.0f} veces lo habitual ({base:.0f})")
    # La salida acompanando a la entrada significa que RESPONDEMOS: ya no es
    # ruido descartado, y ese cambio importa mas que el volumen.
    if kin > 0 and kout > kin * 0.5 and ppm > base * 3:
        motivos.append(f"la salida acompana a la entrada ({kout} KB/min frente a {kin}): "
                       "el servidor esta respondiendo, no descartando")
m.append(ppm)
h["muestras"] = m[-60:]
h["ultima"] = {"ppm": ppm, "kb_in": kin, "kb_out": kout}
json.dump(h, open(est, "w"))
print("\n".join(motivos))
PY

motivos=$(cat /tmp/vigia-salida.txt)
rm -f /tmp/vigia-salida.txt
[ -z "$motivos" ] && exit 0

detalle=$(printf 'El trafico del VPS cambio de forma que merece una mirada.\n\n%s\n\nMedido en %s s: %s paquetes/min, %s KB/min entrando, %s KB/min saliendo.\n\nQue mirar: ss -tn state established | tail -n +2 | wc -l  ·  tcpdump -i %s -nn -c 200 "not port 22"  ·  journalctl -u caddy --since "10 min ago"\n\nSi es escaneo bloqueado el trafico entra y no sale; si sale, algo esta respondiendo.' \
  "$motivos" "$VENTANA" "$ppm" "$kb_in" "$kb_out" "$IFAZ")

if [ -n "$TOKEN_F" ] && [ -f "$TOKEN_F" ] && [ -n "$BASE" ]; then
  python3 - "$TOKEN_F" "$BASE" "$detalle" <<'PY'
import json, sys, urllib.request
tok = open(sys.argv[1]).read().strip()
base, cuerpo = sys.argv[2].rstrip("/"), sys.argv[3]
p = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
     "name": "msg_send", "arguments": {
        "para": "todos", "tipo": "alerta",
        "asunto": "Cambio en el trafico de red del VPS", "cuerpo": cuerpo}}}
r = urllib.request.Request(f"{base}/{tok}/mcp", data=json.dumps(p).encode(),
    headers={"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "User-Agent": "eva-vigia-red/1.0"})
urllib.request.urlopen(r, timeout=30).read()
print("aviso publicado en el canal")
PY
else
  echo "$detalle"
fi
