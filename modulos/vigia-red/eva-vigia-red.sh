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

# Referencia: lo que es normal A ESTA HORA, no una mediana de todo mezclado.
python3 - "$ESTADO" "$ppm" "$kb_in" "$kb_out" <<'FINPY' > /tmp/vigia-salida.txt
import json, os, statistics, sys, datetime
est, ppm, kin, kout = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
hora = datetime.datetime.now(datetime.timezone.utc).hour

h = {"muestras": []}
if os.path.exists(est):
    try: h = json.load(open(est))
    except Exception: pass
# Las muestras de la v1 eran un numero suelto, sin hora ni bytes: no sirven para
# esto y se descartan solas. Se pierde el historial una vez, no cada vez.
m = [x for x in h.get("muestras", []) if isinstance(x, dict)]
motivos = []
mediana = lambda v: statistics.median(v) if v else 0

# ── 1. VOLUMEN, contra la MISMA FRANJA HORARIA ───────────────────────────────
# Una mediana global mezcla el dia con la noche. Medido en este VPS sobre 60
# muestras reales: de dia ~1395 paq/min, de noche ~170. Ocho veces menos. Con la
# mediana global (1221) el vigia se queda SORDO de madrugada — 1200 paq/min a las
# 3am son siete veces lo normal de esa hora y aun asi caen POR DEBAJO del umbral.
franja = [x["ppm"] for x in m if abs((x.get("hora", hora) - hora + 12) % 24 - 12) <= 1]
usa_franja = len(franja) >= 5
base = mediana(franja) if usa_franja else mediana([x["ppm"] for x in m])
n = len(franja) if usa_franja else len(m)
if n >= 5 and base > 0 and ppm > base * 10:
    motivos.append(f"paquetes entrantes {ppm}/min, {ppm/base:.0f} veces lo habitual a esta "
                   f"hora ({base:.0f}, sobre {n} muestras)")

# ── 2. ESTAMOS RESPONDIENDO — y esto ya NO va detras de un umbral de volumen ──
# Este era el aviso que de verdad importa y estaba inutilizado: exigia ademas que
# el volumen triplicase la mediana, y en 2,5 dias de historia real eso no ocurrio
# NI UNA VEZ (maximo 2832 frente a un umbral de 3663). La senal valiosa no podia
# dispararse nunca. Y el caso que tapaba es justo el peor: un intruso competente
# es discreto — poco volumen, mucha respuesta.
# Escaneo que el cortafuegos tira: entra y no sale. Si empieza a salir, alguien
# esta CONTESTANDO.
alto = False
if kin >= 20:                      # suelo: con 2 KB/min la razon es ruido puro
    r = kout / kin
    hist = [x["kb_out"] / x["kb_in"] for x in m if x.get("kb_in", 0) >= 20]
    if len(hist) >= 10:
        base_r = mediana(hist)
        alto = r > max(base_r * 3, 0.5)
        # DOS MUESTRAS SEGUIDAS, no una. En 1-2 sep esta senal salto cuatro veces
        # en dos dias y ninguna era un compromiso: un servidor publico responde al
        # escaneo en la capa TLS y en SSH, y esos bytes salen por la interfaz sin
        # aparecer como contenido servido (Caddy registro 0 KB de cuerpo en esas
        # ventanas). Un barrido es un pico que pasa; una exfiltracion se sostiene.
        # Exigir que se repita cuesta una hora de retraso y evita cuatro falsos
        # positivos — y un vigia al que se ignora ya no es un vigia.
        if alto and m and m[-1].get("razon_alta"):
            motivos.append(f"la salida lleva DOS MEDICIONES acompanando a la entrada: {kout} "
                           f"KB/min frente a {kin} (razon {r:.2f}; lo normal aqui es "
                           f"{base_r:.2f}). Un barrido no se sostiene una hora: mira que hay "
                           "establecido y quien responde")

m.append({"hora": hora, "ppm": ppm, "kb_in": kin, "kb_out": kout, "razon_alta": alto})
h["muestras"] = m[-336:]           # 14 dias a una muestra/hora: cabe el ciclo semanal
h["ultima"] = {"ppm": ppm, "kb_in": kin, "kb_out": kout, "hora": hora,
               "base_franja": base, "muestras_franja": n}
json.dump(h, open(est, "w"))
print("\n".join(motivos))
FINPY

motivos=$(cat /tmp/vigia-salida.txt)
rm -f /tmp/vigia-salida.txt

# ── LATIDO ───────────────────────────────────────────────────────────────────
# Se escribe SIEMPRE, haya alerta o no. Sin esto, un vigia muerto y un vigia que
# no ve nada producen el mismo silencio, y no hay forma de distinguirlos: la
# ausencia de alertas dejaria de ser informacion. Con el latido, el silencio es
# demostrable — la consola muestra "ultima revision: hace X" y se pone en rojo si
# pasan dos horas. No es un mensaje al canal: seria ruido diario. Es un hecho que
# se sobrescribe.
if [ -n "$TOKEN_F" ] && [ -f "$TOKEN_F" ] && [ -n "$BASE" ]; then
  python3 - "$TOKEN_F" "$BASE" "$ppm" "$kb_in" "$kb_out" "$motivos" <<'LATIDO' >/dev/null 2>&1 || true
import json, sys, urllib.request, datetime
tok = open(sys.argv[1]).read().strip()
base = sys.argv[2].rstrip("/")
ppm, kin, kout, motivos = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
valor = (f"revisado {datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')} · "
         f"{ppm} paq/min, {kin} KB/min entrando, {kout} saliendo · "
         + ("SIN NOVEDAD" if not motivos.strip() else "ALERTA EMITIDA: " + motivos.strip().splitlines()[0][:120]))
p = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
     "name": "fact_set", "arguments": {"clave": "vigia.ultimo_chequeo", "valor": valor,
                                       "fuente": "eva-vigia-red (automatico, cada hora)"}}}
r = urllib.request.Request(f"{base}/{tok}/mcp", data=json.dumps(p).encode(),
    headers={"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "User-Agent": "eva-vigia-red/1.0"})
urllib.request.urlopen(r, timeout=30).read()
LATIDO
fi

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
