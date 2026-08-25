#!/bin/bash
# Verifies a from-scratch install of this repo on an isolated instance:
# own port, database, participants file and systemd unit. Checks health,
# the web console and its security headers, and an authenticated call.
# Removes everything at the end unless KEEP=1. Requires sudo and a venv
# with the MCP SDK (reuses an existing one via VENV=).
set -euo pipefail

PREFIX=${PREFIX:-evastate-check}
PORT=${PORT:-8799}
VENV=${VENV:-/opt/evastate/venv}
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
SVC_USER=${SVC_USER:-evastate}
OPT=/opt/$PREFIX; ETC=/etc/$PREFIX; VAR=/var/lib/$PREFIX
FALLOS=0

# llama a un tool del MCP y devuelve el texto del resultado ya desanidado
tool() {
  local tk="$1" nombre="$2" args="${3:-}"
  [ -n "$args" ] || args='{}'
  local cuerpo
  cuerpo=$(python3 -c 'import json,sys; print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":sys.argv[1],"arguments":json.loads(sys.argv[2])}}))' "$nombre" "$args")
  curl -sf -X POST "http://127.0.0.1:$PORT/$tk/mcp" \
    -H 'Content-Type: application/json' -H 'Accept: application/json' -d "$cuerpo" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print("".join(c.get("text","") for c in r["result"]["content"]))'
}

paso() { printf '%-58s' "$1"; }
ok()   { echo "OK"; }
mal()  { echo "FALLO: $1"; FALLOS=$((FALLOS+1)); }

limpiar() {
  sudo systemctl disable --now "$PREFIX" >/dev/null 2>&1 || true
  sudo rm -f "/etc/systemd/system/$PREFIX.service" "/etc/$PREFIX.env"
  sudo rm -rf "$OPT" "$ETC" "$VAR"
  sudo systemctl daemon-reload
}
trap '[ "${KEEP:-0}" = 1 ] || limpiar' EXIT

echo "== instalacion limpia desde $REPO (prefijo $PREFIX, puerto $PORT) =="

paso "1. arboles y permisos"
sudo mkdir -p "$OPT" "$ETC" "$VAR"/{ctl-spool,ctl-out}
sudo chown -R "$SVC_USER:$SVC_USER" "$VAR"
sudo chgrp "$SVC_USER" "$ETC" && sudo chmod 2775 "$ETC"
ok

paso "2. codigo y panel instalados"
sudo install -m 644 "$REPO/server.py" "$OPT/server.py"
sudo install -m 644 "$REPO/panel.html" "$OPT/panel.html"
sudo install -m 755 "$REPO/scripts/participante.py" "$OPT/participante.py"
echo "{}" | sudo tee "$ETC/participants.json" >/dev/null
sudo chgrp "$SVC_USER" "$ETC/participants.json" && sudo chmod 660 "$ETC/participants.json"
ok

paso "3. config por entorno (config.example.env)"
sudo tee "/etc/$PREFIX.env" >/dev/null <<ENV
EVASTATE_DB=$VAR/state.db
EVASTATE_PORT=$PORT
EVASTATE_PARTICIPANTS=$ETC/participants.json
EVASTATE_PANEL=$OPT/panel.html
EVASTATE_SERVICE=$PREFIX
EVASTATE_SPOOL=$VAR/ctl-spool
EVASTATE_CTL_OUT=$VAR/ctl-out
EVASTATE_SITES=$VAR/sites
EVASTATE_APPS=$VAR/apps
ENV
ok

paso "4. unidad systemd"
sudo tee "/etc/systemd/system/$PREFIX.service" >/dev/null <<UNIT
[Unit]
Description=Shared-state MCP server (install check)
After=network-online.target
[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
EnvironmentFile=/etc/$PREFIX.env
ExecStart=$VENV/bin/python $OPT/server.py
Restart=no
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$VAR $ETC
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl start "$PREFIX"
sleep 3
systemctl is-active --quiet "$PREFIX" && ok || mal "el servicio no arranco"

paso "5. /health responde"
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && ok || mal "sin respuesta"

paso "6. alta del primer participante + autoridad"
TOK=$(sudo EVASTATE_PARTICIPANTS="$ETC/participants.json" EVASTATE_SERVICE="$PREFIX" \
      python3 "$OPT/participante.py" alta jefe humano "Admin" local | sed -n '2p')
sudo EVASTATE_PARTICIPANTS="$ETC/participants.json" EVASTATE_SERVICE="$PREFIX" \
      python3 "$OPT/participante.py" autoridad jefe on >/dev/null
sleep 3
[ ${#TOK} -ge 24 ] && ok || mal "no se obtuvo token"

paso "7. el archivo NO guarda el token en claro"
if sudo grep -qF "$TOK" "$ETC/participants.json"; then mal "token en texto plano"; else ok; fi

paso "8. /panel sirve la consola"
# nota: guardar la respuesta antes de filtrarla; con pipefail, `curl | grep -q`
# puede fallar por SIGPIPE cuando grep cierra el pipe antes de que curl termine
PAGINA=$(curl -sf "http://127.0.0.1:$PORT/panel" || true)
case "$PAGINA" in *"Panel de state"*) ok ;; *) mal "el panel no responde" ;; esac

paso "9. cabeceras de seguridad del panel"
H=$(curl -sfD- -o /dev/null "http://127.0.0.1:$PORT/panel" || true)
falta=""
for c in "default-src 'none'" "frame-ancestors 'none'" "cache-control: no-store" "x-content-type-options: nosniff"; do
  case "$(echo "$H" | tr 'A-Z' 'a-z')" in *"$c"*) ;; *) falta="$falta [$c]" ;; esac
done
[ -z "$falta" ] && ok || mal "faltan:$falta"

paso "10. llamada autenticada (whoami) y autoridad activa"
W=$(tool "$TOK" whoami || true)
case "$W" in *'"jefe"'*) case "$W" in *'"autoridad": true'*) ok ;; *) mal "sin autoridad: $W" ;; esac ;;
   *) mal "whoami: $W" ;; esac

paso "11. token invalido -> 404 (no filtra)"
C=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/token-que-no-existe-0000000000/mcp" \
    -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
[ "$C" = "404" ] && ok || mal "codigo $C"

paso "12. la autoridad puede publicar en la cartelera"
P=$(tool "$TOK" cartel_publicar '{"tipo":"regla","asunto":"instalacion","cuerpo":"verificacion"}' || true)
case "$P" in *CART-001*) ok ;; *) mal "no publico: $P" ;; esac

echo
if [ "$FALLOS" = 0 ]; then echo "RESULTADO: instalacion limpia verificada (12/12)"; else
  echo "RESULTADO: $FALLOS fallo(s)"; fi
[ "${KEEP:-0}" = 1 ] && echo "(instancia conservada: KEEP=1)"
exit "$FALLOS"
