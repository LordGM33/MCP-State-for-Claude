# Operación e instalación

## Requisitos

Ubuntu ≥ 24.04, Caddy ≥ 2.10, Python ≥ 3.12 con venv, systemd. DNS: apex +
wildcard `*.dominio` apuntando al servidor (probado tras Cloudflare).

## Instalación (resumen del despliegue real)

    # usuario de servicio y árboles
    sudo useradd -r -s /usr/sbin/nologin evastate
    sudo mkdir -p /opt/evastate /var/lib/evastate/{ctl-spool,ctl-out} \
                  /var/www/sites /srv/apps /var/backups/evastate /etc/caddy/apps.d
    sudo chown evastate:evastate /var/www/sites /srv/apps \
                  /var/lib/evastate/ctl-spool /var/lib/evastate/ctl-out
    # venv + SDK
    sudo python3 -m venv /opt/evastate/venv
    sudo /opt/evastate/venv/bin/pip install "mcp>=2" starlette uvicorn
    # código
    sudo install -m644 server.py /opt/evastate/
    sudo install -m755 scripts/eva-app-ctl scripts/eva-backup.py /usr/local/sbin/
    sudo install -m755 scripts/participante.py /opt/evastate/
    sudo install -m644 systemd/* /etc/systemd/system/
    # entorno (sin token global: la auth es por participante)
    echo "EVASTATE_DB=/var/lib/evastate/state.db" | sudo tee /etc/evastate.env
    echo "EVASTATE_PORT=8787" | sudo tee -a /etc/evastate.env
    echo "{}" | sudo tee /etc/evastate/participants.json
    # caddy: adaptar caddy/Caddyfile.ejemplo a tu dominio
    sudo systemctl daemon-reload
    sudo systemctl enable --now evastate eva-appd.path eva-backup.timer
    sudo systemctl reload caddy

## Altas de participantes (solo admin)

    sudo /opt/evastate/venv/bin/python /opt/evastate/participante.py alta <id> <tipo> "<Nombre>" <maquina>
    # imprime el token UNA vez; repartirlo por canal seguro (archivo local)
    # también: baja <id> · rotar <id> · lista

## Verificación mínima tras instalar (la vara del 23-ago fue 7/7)

1. `curl https://state.<dominio>/health` → `ok · N registros`
2. `initialize` + `tools/call whoami` con dos tokens → identidades distintas
3. token inválido → 404
4. solicitud→inbox→respuesta→hilo (`SOL-001`)
5. claim + PUT estático → subdominio sirve con TLS emitido on-demand
6. PUT app dinámica → `active` y proxied
7. `systemctl start eva-backup` → gz con `integrity_check` ok
8. `GET /<TOKEN>/backup` -> gz mas reciente con cabecera `X-Backup-Sha256`
   (cada participante baja su copia al abrir sesion)
9. `msg_send(tipo=respuesta, responde_a='archivo:<fecha>')` -> enlaza hilos
   migrados de los archivos sin cerrar ninguna SOL del canal

## Averías conocidas

- MCP responde 421: falta el host público en `allowed_hosts` de
  `TransportSecuritySettings` (protección anti DNS-rebinding del SDK).
- 403 solo desde Python: User-Agent (Cloudflare). (User-Agent propio obligatorio).
- Subdominio sin cert y sin log: nombre listado junto a un wildcard (no listar nombres individuales junto al wildcard).
- App en 502: casi siempre la app (mirar `app_logs`); systemd la reintenta.
