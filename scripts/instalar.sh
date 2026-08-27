#!/usr/bin/env bash
# Installs the state channel in one of two modes. Run from the repository root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=${PREFIX:-evastate}
OPT=/opt/$PREFIX; ETC=/etc/$PREFIX; VAR=/var/lib/$PREFIX
PORT=${PORT:-8787}
# Derivados de PREFIX para que dos instalaciones no se pisen (lo usa la prueba).
SITIOS=${SITIOS:-/var/www/sites}
APPS=${APPS:-/srv/apps}
BACKUPS=${BACKUPS:-/var/backups/$PREFIX}

rojo(){ printf '\033[31m%s\033[0m\n' "$*"; }
verde(){ printf '\033[32m%s\033[0m\n' "$*"; }
aviso(){ printf '\033[33m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- platform
so=$(uname -s 2>/dev/null || echo desconocido)
arq=$(uname -m 2>/dev/null || echo desconocido)
case "$arq" in
  x86_64|amd64) arq_caddy=amd64 ;;
  aarch64|arm64) arq_caddy=arm64 ;;
  armv7l|armv6l) arq_caddy=armv7 ;;
  *) arq_caddy="" ;;
esac

echo "Sistema detectado: $so $arq"
if [ "$so" != "Linux" ]; then
  rojo "Este instalador cubre Linux con systemd."
  echo
  echo "En macOS o Windows el canal funciona igualmente, pero la instalacion"
  echo "es manual y el modo con proxy no aplica. Ver docs/OPERATIONS.md,"
  echo "seccion 'Running without a reverse proxy'. Lo soportado por plataforma:"
  echo
  echo "  Linux + systemd  todo, incluidos subdominios y apps dinamicas"
  echo "  macOS            canal, consola, sitios estaticos y TLS propio"
  echo "  Windows          canal, consola, sitios estaticos y TLS propio"
  echo "                   (sin apps dinamicas ni scale-to-zero: necesitan systemd)"
  exit 1
fi
command -v systemctl >/dev/null || { rojo "Hace falta systemd."; exit 1; }
command -v python3 >/dev/null || { rojo "Hace falta Python 3.12 o superior."; exit 1; }
py=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)' \
  || { rojo "Python $py: hace falta 3.12 o superior."; exit 1; }
verde "Python $py, systemd presente."

# ---------------------------------------------------------------- mode
modo=${MODO:-}
if [ -z "$modo" ]; then
  echo
  echo "Donde va a vivir este servidor?"
  echo
  echo "  1) Expuesto a internet, con dominio propio"
  echo "     Caddy delante, certificados publicos, subdominios reales."
  echo "     Se recomienda ademas un CDN con las reglas de docs/OPERATIONS.md."
  echo
  echo "  2) Red local privada (LAN)"
  echo "     Sin proxy y sin DNS: el servidor sirve su propio HTTPS con una"
  echo "     autoridad local, y los sitios se publican en /s/<nombre>/."
  echo
  read -rp "Elige 1 o 2: " r
  case "$r" in 1) modo=web ;; 2) modo=lan ;; *) rojo "Respuesta no valida."; exit 1 ;; esac
fi

if [ "$modo" = "lan" ]; then
  echo
  aviso "AVISO sobre el modo LAN — leelo antes de seguir:"
  echo "  · No hay CDN delante. El filtro de escaneo y el limite del borde"
  echo "    no existen aqui; el unico freno es el limite por identidad del"
  echo "    propio servidor."
  echo "  · La seguridad de esto descansa en que la red local sea de confianza."
  echo "    Si esa red deja de serlo, esto no es una defensa."
  echo "  · Cada cliente tendra que instalar la raiz de la autoridad local."
  echo
  read -rp "Entendido? [escribe: si] " ok
  [ "$ok" = "si" ] || { echo "Instalacion cancelada."; exit 1; }
fi

# ---------------------------------------------------------------- names
if [ "$modo" = "web" ]; then
  read -rp "Dominio (ej. example.com): " DOMINIO
  [ -n "$DOMINIO" ] || { rojo "El dominio es obligatorio en modo web."; exit 1; }
  PUBLIC_HOST="state.$DOMINIO"
else
  ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
  [ -n "$ip" ] || ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  read -rp "IP de este servidor en la LAN [$ip]: " r; ip=${r:-$ip}
  [ -n "$ip" ] || { rojo "No se pudo determinar la IP."; exit 1; }
  DOMINIO=${DOMINIO:-state.lan}
  # El SDK responde 421 a cualquier Host que no figure aqui: el puerto importa.
  PUBLIC_HOST="$ip:$PORT"
fi

# ---------------------------------------------------------------- caddy
if [ "$modo" = "web" ]; then
  if command -v caddy >/dev/null; then
    verde "Caddy ya instalado: $(caddy version | head -1)"
  else
    echo "Instalando Caddy desde su repositorio oficial..."
    if command -v apt-get >/dev/null; then
      sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
      sudo apt-get update -qq && sudo apt-get install -y caddy
      verde "Caddy instalado. Se actualizara con el resto del sistema."
    else
      rojo "Sin apt no puedo instalar Caddy automaticamente."
      echo "Instalalo desde https://caddyserver.com/docs/install (arquitectura: $arq_caddy)"
      echo "y vuelve a ejecutar este script."
      exit 1
    fi
  fi
fi

# La unidad usa PrivateTmp: dentro del servicio /tmp es otro. Una ruta ahi
# hace fallar el arranque con un 226/NAMESPACE que no explica nada.
for d in "$VAR" "$SITIOS" "$APPS" "$BACKUPS"; do
  case "$d" in /tmp/*|/var/tmp/*)
    rojo "Ruta no valida: $d"
    echo "El servicio corre con PrivateTmp, asi que no vera nada bajo /tmp."
    echo "Usa una ruta permanente."
    exit 1 ;;
  esac
done

# ---------------------------------------------------------------- install
echo "Instalando el canal en $OPT..."
id -u "$PREFIX" >/dev/null 2>&1 || sudo useradd -r -s /usr/sbin/nologin "$PREFIX"
sudo mkdir -p "$OPT" "$ETC" "$VAR"/{ctl-spool,ctl-out} "$SITIOS" "$APPS" "$BACKUPS"
sudo chown -R "$PREFIX:$PREFIX" "$VAR" "$SITIOS" "$APPS" "$BACKUPS"
sudo chgrp "$PREFIX" "$ETC" && sudo chmod 2775 "$ETC"

sudo python3 -m venv "$OPT/venv"
sudo "$OPT/venv/bin/pip" install -q "mcp>=2" starlette uvicorn
sudo install -m644 "$REPO/server.py" "$REPO/panel.html" "$OPT/"
sudo install -m755 "$REPO/scripts/participante.py" "$OPT/"
[ -f "$ETC/participants.json" ] || { echo "{}" | sudo tee "$ETC/participants.json" >/dev/null; }
sudo chgrp "$PREFIX" "$ETC/participants.json" && sudo chmod 660 "$ETC/participants.json"

# ---------------------------------------------------------------- local CA
if [ "$modo" = "lan" ]; then
  echo "Generando la autoridad local y el certificado del servidor..."
  sudo mkdir -p "$ETC/pki"
  if [ ! -f "$ETC/pki/ca.crt" ]; then
    sudo openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
      -keyout "$ETC/pki/ca.key" -out "$ETC/pki/ca.crt" \
      -subj "/CN=state local CA ($DOMINIO)" \
      -addext "basicConstraints=critical,CA:TRUE" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
    sudo chmod 600 "$ETC/pki/ca.key"
  fi
  # El certificado lleva la IP: asi no hace falta DNS en la LAN.
  sudo openssl req -newkey rsa:2048 -nodes -keyout "$ETC/pki/srv.key" \
    -out "$ETC/pki/srv.csr" -subj "/CN=state" 2>/dev/null
  printf 'subjectAltName=DNS:state,DNS:%s,DNS:localhost,IP:127.0.0.1,IP:%s\nextendedKeyUsage=serverAuth\n' \
    "$DOMINIO" "$ip" | sudo tee "$ETC/pki/ext.cnf" >/dev/null
  sudo openssl x509 -req -in "$ETC/pki/srv.csr" -CA "$ETC/pki/ca.crt" -CAkey "$ETC/pki/ca.key" \
    -CAcreateserial -out "$ETC/pki/srv.crt" -days 825 -sha256 -extfile "$ETC/pki/ext.cnf" 2>/dev/null
  sudo chmod 600 "$ETC/pki/srv.key"; sudo chgrp "$PREFIX" "$ETC/pki/srv.key" "$ETC/pki/srv.crt"
  sudo chmod 640 "$ETC/pki/srv.key"
  verde "Autoridad local creada. Raiz a repartir: $ETC/pki/ca.crt"
fi

# ---------------------------------------------------------------- config
sudo tee "/etc/$PREFIX.env" >/dev/null <<ENV
EVASTATE_MODO=$modo
EVASTATE_DOMAIN=$DOMINIO
EVASTATE_PUBLIC_HOST=$PUBLIC_HOST
EVASTATE_PORT=$PORT
EVASTATE_DB=$VAR/state.db
EVASTATE_PARTICIPANTS=$ETC/participants.json
EVASTATE_SITES=$SITIOS
EVASTATE_APPS=$APPS
EVASTATE_SPOOL=$VAR/ctl-spool
EVASTATE_CTL_OUT=$VAR/ctl-out
EVASTATE_BACKUP_DIR=$BACKUPS
EVASTATE_PANEL=$OPT/panel.html
ENV
if [ "$modo" = "lan" ]; then
  sudo tee -a "/etc/$PREFIX.env" >/dev/null <<ENV
EVASTATE_SERVE_SITES=1
EVASTATE_BIND=0.0.0.0
EVASTATE_TLS_CERT=$ETC/pki/srv.crt
EVASTATE_TLS_KEY=$ETC/pki/srv.key
ENV
fi
sudo chmod 644 "/etc/$PREFIX.env"

sudo tee "/etc/systemd/system/$PREFIX.service" >/dev/null <<UNIT
[Unit]
Description=state coordination channel
After=network.target
[Service]
User=$PREFIX
Group=$PREFIX
EnvironmentFile=/etc/$PREFIX.env
ExecStart=$OPT/venv/bin/python $OPT/server.py
Restart=on-failure
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=$VAR $SITIOS $APPS $BACKUPS $ETC
[Install]
WantedBy=multi-user.target
UNIT

# Lanzador de administracion: fija el entorno de ESTA instalacion. Invocar
# participante.py a pelo tomaria los valores por defecto, que pueden ser los de
# otra instalacion de la misma maquina.
sudo tee "$OPT/statectl" >/dev/null <<CTL
#!/usr/bin/env bash
set -euo pipefail
export EVASTATE_ENV=/etc/$PREFIX.env
export EVASTATE_SERVICE=$PREFIX
export EVASTATE_GROUP=$PREFIX
exec $OPT/venv/bin/python $OPT/participante.py "\$@"
CTL
sudo chmod 755 "$OPT/statectl"

sudo systemctl daemon-reload
sudo systemctl enable --now "$PREFIX"
sleep 3
systemctl is-active --quiet "$PREFIX" || { rojo "El servicio no arranco:"; sudo journalctl -u "$PREFIX" -n 20 --no-pager; exit 1; }
verde "Servicio activo."

# ---------------------------------------------------------------- done
echo
verde "Instalado en modo $modo."
echo
echo "Siguiente paso: registrar la primera identidad, que sera la autoridad."
echo
echo "  sudo $OPT/statectl alta <id> cowork \"<Nombre>\" <maquina>"
echo "  sudo $OPT/statectl autoridad <id> on"
echo
if [ "$modo" = "web" ]; then
  echo "Y adaptar caddy/Caddyfile.example a tu dominio antes de recargar Caddy."
  echo "Comprobacion:  curl https://$PUBLIC_HOST/health"
else
  echo "Comprobacion:  curl --cacert $ETC/pki/ca.crt https://$PUBLIC_HOST/health"
  echo
  echo "Cada cliente necesita la raiz $ETC/pki/ca.crt para verificar el"
  echo "certificado. El texto que genera alta_invitar ya la incluye."
  aviso "Recuerda: en modo LAN no hay CDN. El unico limite es el del servidor."
fi
