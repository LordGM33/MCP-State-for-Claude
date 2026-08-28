#!/usr/bin/env bash
# Instala el modulo vigia-red. Ver MODULO.md.
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=${PREFIX:-evastate}
ETC=/etc/$PREFIX; VAR=/var/lib/$PREFIX
UNIDAD=eva-vigia-red

rojo(){ printf '\033[31m%s\033[0m\n' "$*"; }
verde(){ printf '\033[32m%s\033[0m\n' "$*"; }

if [ "${1:-}" = "--desinstalar" ]; then
  sudo systemctl disable --now "$UNIDAD.timer" 2>/dev/null || true
  sudo rm -f "/etc/systemd/system/$UNIDAD.service" "/etc/systemd/system/$UNIDAD.timer"
  sudo rm -f /usr/local/sbin/eva-vigia-red "$VAR/vigia-red.json"
  sudo systemctl daemon-reload
  verde "Modulo retirado."
  exit 0
fi

command -v systemctl >/dev/null || { rojo "Este modulo necesita systemd."; exit 1; }

IFAZ=${EVA_IFAZ:-$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')}
IFAZ=${IFAZ:-eth0}
[ -d "/sys/class/net/$IFAZ" ] || { rojo "No existe la interfaz $IFAZ. Define EVA_IFAZ."; exit 1; }

cat <<TEXTO

MODULO vigia-red — avisa solo cuando el trafico cambia de forma que importa

LO QUE VA A PODER HACER:
  · Leer los contadores de paquetes de la interfaz $IFAZ.
  · Guardar sus ultimas 60 muestras en $VAR/vigia-red.json

LO QUE NO VA A PODER HACER:
  · Ver el CONTENIDO del trafico. Cuenta paquetes y bytes, no los inspecciona.
  · Cambiar nada del sistema.

No necesita ninguna credencial para funcionar.

TEXTO
read -rp "Continuar? [escribe: si] " ok
[ "$ok" = "si" ] || { echo "Cancelado."; exit 1; }

echo
echo "Puede publicar sus avisos en el canal, o dejarlos en el registro del sistema."
echo "Si eliges el canal, dale identidad PROPIA (un participante de tipo servicio):"
echo "un aviso debe ir firmado por quien lo levanta, no por una persona."
read -rp "Ruta de un fichero con su token, o Enter para usar el registro: " TOKF

sudo install -m755 "$AQUI/eva-vigia-red.sh" /usr/local/sbin/eva-vigia-red
sudo mkdir -p "$ETC"
if [ -n "${TOKF:-}" ] && [ -f "$TOKF" ]; then
  read -rp "URL base del canal (https://state.ejemplo.com): " URL
  sudo install -o root -g "$PREFIX" -m 640 "$TOKF" "$ETC/vigia.token"
  sudo tee "$ETC/vigia.env" >/dev/null <<ENV
EVA_IFAZ=$IFAZ
EVA_VIGIA_TOKEN=$ETC/vigia.token
EVA_VIGIA_URL=$URL
ENV
  echo "  publicara en el canal"
else
  sudo tee "$ETC/vigia.env" >/dev/null <<ENV
EVA_IFAZ=$IFAZ
ENV
  echo "  escribira en el registro del sistema (journalctl -u $UNIDAD)"
fi
sudo chmod 644 "$ETC/vigia.env"

sudo tee "/etc/systemd/system/$UNIDAD.service" >/dev/null <<UNIT
[Unit]
Description=Vigia del trafico: avisa solo si cambia de forma que importa
[Service]
Type=oneshot
Environment=EVA_VIGIA_SEG=120
Environment=EVA_VIGIA_ESTADO=$VAR/vigia-red.json
EnvironmentFile=-$ETC/vigia.env
ExecStart=/usr/local/sbin/eva-vigia-red
UNIT

sudo tee "/etc/systemd/system/$UNIDAD.timer" >/dev/null <<UNIT
[Unit]
Description=Vigia del trafico, cada hora
[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true
[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIDAD.timer"
echo
verde "Modulo instalado en la interfaz $IFAZ."
echo "Estara callado hasta tener cinco muestras: necesita saber que es normal aqui."
systemctl list-timers "$UNIDAD" --no-legend --no-pager | awk '{print "  primera medicion: "$1" "$2" "$3}'
echo "Retirarlo: bash modulos/vigia-red/instalar.sh --desinstalar"
