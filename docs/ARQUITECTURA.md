# Arquitectura

## Vista general

    cliente (cowork/agente, cualquier máquina)
        │  HTTPS  POST /<TOKEN>/mcp          (JSON-RPC, stateless)
        │         PUT  /<TOKEN>/deploy/<n>   (tar.gz sitio estático)
        │         PUT  /<TOKEN>/app/<n>      (tar.gz app dinámica)
        ▼
    Cloudflare (proxy, filtro de bots)
        ▼
    Caddy :443  ── on-demand TLS ──►  GET 127.0.0.1:8787/tls-check
        │ reverse_proxy state → 127.0.0.1:8787
        │ file_server  <n>.dominio → /var/www/sites/<n>/
        │ import apps.d/*.caddy  → reverse_proxy 127.0.0.1:<puerto app>
        ▼
    evastate (server.py, usuario sin privilegios, sandbox systemd)
        │ SQLite WAL  /var/lib/evastate/state.db   (tabla única items:
        │   kind + key + data JSON — el "esquema" vive en los datos)
        │ spool ────► eva-appd.path (root) ────► eva-app-ctl
        ▼                                        (unidades + snippets Caddy)
    apps dinámicas: eva-app-<n>.service, DynamicUser, 512M/80% CPU

## Las cinco decisiones que definen el diseño

**1. Identidad sellada por el servidor.** El token viaja en la ruta
(`/<TOKEN>/mcp`); un despachador ASGI resuelve token→identidad ANTES de
entrar al MCP y la fija en un `contextvars.ContextVar`. Ninguna herramienta
acepta un parámetro `de`: la firma es del servidor. Consecuencia: suplantar
exige robar el token, no basta con mentir en un argumento.

**2. Tabla única, esquema en los datos.** `items(kind, key, data-JSON)` con
upsert por `(kind, key)` y append para lo secuencial. Migrar el esquema de
mensajes no toca DDL. El coste (sin FKs ni índices por campo) es aceptable a
esta escala (decenas de participantes, miles de mensajes).

**3. Privilegios por spool, no por sudo.** `evastate` corre con
`ProtectSystem=strict` + `NoNewPrivileges`; bajo ese sandbox sudo NO puede
escribir /etc (hereda el namespace de solo-lectura). Las operaciones root
(instalar unidades de apps, recargar Caddy) se piden dejando un JSON en un
spool; una unidad `.path` de systemd despierta al ayudante root
(`eva-app-ctl`), que valida TODO de nuevo — el ayudante es la frontera de
seguridad, no confía en quien pide.

**4. TLS on-demand con compuerta.** Caddy solo emite certificado para un
subdominio si `/tls-check` lo aprueba, y este solo aprueba subdominios
RESERVADOS en la base. Nadie puede provocar emisiones apuntando DNS ajeno al
servidor. Lección aprendida (aprendida en el despliegue original): un nombre individual
listado en un bloque propio Y cubierto por un wildcard queda SIN certificado
y sin log — por eso el apex va solo y todo lo demás es wildcard+on-demand.

**5. Apps dinámicas = unidad systemd endurecida, no contenedor.** Sin Docker:
`DynamicUser=yes`, sin privilegios, FS de solo lectura salvo su carpeta,
límites de RAM/CPU/tareas, reinicio automático. El manifiesto `eva-app.toml`
declara `cmd` (1 línea, sin comillas simples, ≤300 chars — validado en el
ayudante root) y el proceso escucha en `$PORT` asignado por el servidor.

## Flujo de un mensaje

`msg_send(para='agente-b', tipo='solicitud', ...)` → identidad del token
como `de` → ref `SOL-NNN` de una secuencia persistida → estado `abierta` →
`msg_inbox()` de producción lo lista → su `msg_send(tipo='respuesta',
responde_a='SOL-NNN')` marca la solicitud `respondida` → `msg_hilo(ref)`
devuelve el hilo completo. `msg_desde(fecha)` da el delta desde una fecha.

## Participantes

`/etc/evastate/participants.json` (root:evastate 640) es la fuente de verdad
de tokens; se espeja SIN tokens en la base (kind `participant`) para que sea
consultable. Altas/bajas/rotación con `scripts/participante.py` (solo admin,
reinicia el servicio). Tipos: cowork | agente | servicio | humano — el mismo
espacio de nombres sirve para N agentes efímeros (diseño pedido por el
cowork de producción para el protocolo Caín).
