#!/usr/bin/env bash
# Instala el modulo scrum-drive. Lee MODULO.md antes: declara lo que exige.
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=${PREFIX:-evastate}
OPT=/opt/$PREFIX; ETC=/etc/$PREFIX; VAR=/var/lib/$PREFIX
UNIDAD=eva-scrum-doc

rojo(){ printf '\033[31m%s\033[0m\n' "$*"; }
verde(){ printf '\033[32m%s\033[0m\n' "$*"; }
aviso(){ printf '\033[33m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- uninstall
if [ "${1:-}" = "--desinstalar" ]; then
  sudo systemctl disable --now "$UNIDAD.timer" 2>/dev/null || true
  sudo systemctl disable --now "$UNIDAD.service" 2>/dev/null || true
  sudo rm -f "/etc/systemd/system/$UNIDAD.service" "/etc/systemd/system/$UNIDAD.timer"
  sudo rm -f /usr/local/sbin/eva-scrum-doc
  sudo systemctl daemon-reload
  verde "Modulo retirado. El canal sigue igual."
  if [ -f "$ETC/google-scrum.json" ]; then
    echo
    aviso "La credencial sigue en $ETC/google-scrum.json"
    echo "No la borro yo: revocarla tiene efecto fuera de esta maquina."
    echo "Si ya no la vas a usar, borrala aqui Y revocala en Google Cloud —"
    echo "borrar el fichero no invalida la clave."
  fi
  exit 0
fi

# ---------------------------------------------------------------- checks
[ -d "$OPT" ] || { rojo "No encuentro el canal en $OPT. Instalalo primero."; exit 1; }
[ -f "$VAR/state.db" ] || { rojo "No encuentro la base en $VAR/state.db."; exit 1; }
command -v systemctl >/dev/null || { rojo "Este modulo necesita systemd."; exit 1; }

# ---------------------------------------------------------------- consent
cat <<'TEXTO'

MODULO scrum-drive — publica el registro de compromisos en un documento de Drive

LO QUE VA A PODER HACER:
  · Leer la base del canal. Necesita permiso de ESCRITURA sobre su directorio,
    porque SQLite en modo WAL mantiene sus ficheros auxiliares incluso para
    consultar. Se compensa con query_only: no puede modificar datos.
  · Escribir en UN documento de Google Drive, el que le indiques, mas cualquier
    otro que alguien comparta con esa misma cuenta de servicio.

LO QUE NO VA A PODER HACER:
  · Escribir en el canal. No abre conexion MCP ni tiene token de participante.
  · Leer nada de Drive que no se haya compartido con su cuenta de servicio.

LO QUE TIENES QUE TENER LISTO:
  · Un fichero JSON de clave de cuenta de servicio de Google.
  · El id del documento, y ese documento COMPARTIDO como Editor con el correo
    de la cuenta de servicio.

ADVERTENCIA: una clave de cuenta de servicio no caduca ni se rota sola. Si se
filtra, funciona hasta que alguien la revoque. Mantenla fuera de todo repo.

El detalle completo, incluido por que hace falta el scope 'drive' y no
'drive.file', esta en modulos/scrum-drive/MODULO.md

TEXTO
read -rp "Continuar? [escribe: si] " ok
[ "$ok" = "si" ] || { echo "Cancelado."; exit 1; }

# ---------------------------------------------------------------- inputs
read -rp "Ruta del JSON de la cuenta de servicio: " CRED
sudo test -f "$CRED" || { rojo "No existe: $CRED"; exit 1; }
# Con sudo porque una credencial ya instalada esta en 640 root:grupo y el
# usuario que ejecuta el instalador no tiene por que poder leerla.
sudo python3 - "$CRED" <<'PY' || exit 1
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    sys.exit(f"  no es un JSON valido: {e}")
if d.get("type") != "service_account":
    sys.exit(f"  no es una clave de cuenta de servicio (type={d.get('type')})")
if not d.get("private_key") or not d.get("client_email"):
    sys.exit("  al JSON le faltan campos: no parece una clave completa")
print(f"  cuenta: {d['client_email']}")
print(f"  proyecto: {d.get('project_id')}")
print()
print("  COMPARTE el documento con ese correo, como Editor, antes de seguir.")
PY

read -rp "Id del documento de Drive: " DOC
[ -n "$DOC" ] || { rojo "El id es obligatorio."; exit 1; }

# ---------------------------------------------------------------- install
echo "Instalando dependencias en el venv del canal..."
sudo "$OPT/venv/bin/pip" install -q google-api-python-client google-auth

sudo install -m755 "$AQUI/eva-scrum-doc.py" /usr/local/sbin/eva-scrum-doc
sudo mkdir -p "$ETC"
# Reinstalar apuntando a la credencial ya instalada es un caso normal.
if [ "$(readlink -f "$CRED")" != "$(readlink -f "$ETC/google-scrum.json")" ]; then
  sudo install -o root -g "$PREFIX" -m 640 "$CRED" "$ETC/google-scrum.json"
else
  sudo chown root:"$PREFIX" "$ETC/google-scrum.json"; sudo chmod 640 "$ETC/google-scrum.json"
  echo "  la credencial ya estaba en su sitio; solo reajusto permisos"
fi
# Si ya habia una configuracion buena, la guardo: una instalacion que falla no
# puede dejar el modulo peor de como estaba.
PREVIA=""
if [ -f "$ETC/scrum.env" ]; then
  PREVIA=$(mktemp); sudo cat "$ETC/scrum.env" > "$PREVIA"
fi
sudo tee "$ETC/scrum.env" >/dev/null <<ENV
EVA_GOOGLE_CRED=$ETC/google-scrum.json
EVA_SCRUM_DOC_ID=$DOC
ENV
sudo chmod 644 "$ETC/scrum.env"

sudo tee "/etc/systemd/system/$UNIDAD.service" >/dev/null <<UNIT
[Unit]
Description=Publica el registro de compromisos en un documento de Drive
After=network-online.target
[Service]
Type=oneshot
EnvironmentFile=/etc/$PREFIX.env
EnvironmentFile=-$ETC/scrum.env
ExecStart=$OPT/venv/bin/python /usr/local/sbin/eva-scrum-doc --drive
User=$PREFIX
Group=$PREFIX
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadOnlyPaths=$ETC
ReadWritePaths=$VAR
UNIT

sudo tee "/etc/systemd/system/$UNIDAD.timer" >/dev/null <<UNIT
[Unit]
Description=Publica el scrum una vez al dia
[Timer]
OnCalendar=${EVA_SCRUM_HORA:-*-*-* 04:07:00 UTC}
Persistent=true
[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload

# ---------------------------------------------------------------- verify
echo
echo "Probando la publicacion antes de dejarlo programado..."
if sudo systemctl start "$UNIDAD.service"; then
  verde "Publicado correctamente."
  sudo journalctl -u "$UNIDAD" -n 3 --no-pager -o cat | grep -i actualizado | sed 's/^/  /' || true
  sudo systemctl enable --now "$UNIDAD.timer"
  echo
  systemctl list-timers "$UNIDAD" --no-legend --no-pager | awk '{print "  proxima publicacion: "$1" "$2" "$3}'
  echo
  [ -n "$PREVIA" ] && rm -f "$PREVIA"
  verde "Modulo instalado."
  echo "Retirarlo: bash modulos/scrum-drive/instalar.sh --desinstalar"
else
  rojo "La primera publicacion fallo."
  sudo journalctl -u "$UNIDAD" -n 12 --no-pager -o cat | sed 's/^/  /'
  # Desactivar de verdad: si venia activo de una instalacion anterior, dejarlo
  # correr publicaria contra la configuracion recien escrita, que no funciona.
  sudo systemctl disable --now "$UNIDAD.timer" 2>/dev/null || true
  if [ -n "$PREVIA" ]; then
    sudo cp "$PREVIA" "$ETC/scrum.env"; sudo chmod 644 "$ETC/scrum.env"; rm -f "$PREVIA"
    aviso "Restaurada la configuracion anterior y detenido el temporizador."
    echo "Si antes funcionaba, vuelve a lanzarlo con: sudo systemctl enable --now $UNIDAD.timer"
  else
    aviso "Temporizador detenido. La configuracion queda escrita pero sin usarse."
  fi
  echo
  echo "Lo mas habitual:"
  echo "  · 404: el documento no esta compartido con la cuenta de servicio,"
  echo "    o se compartio con otra direccion. Revisa el correo exacto."
  echo "  · 403: falta habilitar la Google Drive API en el proyecto."
  exit 1
fi
