# MCP-State-for-Claude

Servidor **MCP remoto de estado compartido** para equipos de agentes (Claude
u otros clientes MCP) que coordinan trabajo sobre un mismo proyecto desde
máquinas distintas. Sustituye los archivos de intercambio en disco por un
canal con identidad, referencias estables e historial — y añade despliegue de
demos y apps a subdominios con TLS automático.

## Qué resuelve

Varios agentes y un humano coordinándose por archivos `.md` en un solo PC:
funciona, pero no cruza máquinas y las solicitudes se pierden si nadie relee
el archivo entero. Este MCP da:

- **Identidad por participante**: cada agente tiene su token; el servidor
  sella cada escritura con la identidad del token — nadie puede escribir como
  otro (no existe ningún parámetro `de`).
- **Mensajería con referencias**: solicitudes con ref estable (`SOL-001`,
  normalizada: `SOL-1` ≡ `SOL-001`), estados abierta/respondida/descartada,
  hilos, bandeja por identidad (sin los envíos propios) y una serie `TEST-N`
  aparte para pruebas que no toca el contador real.
- **Decisiones y hechos canónicos**: append-only con `supersede`; datos que
  todos deben citar igual.
- **Despliegue sin SSH**: sitios estáticos y apps dinámicas (sandbox systemd
  con límites de memoria/CPU) a `<nombre>.<tu-dominio>` con certificado TLS
  emitido on-demand.
- **Respaldo verificable**: diario en el servidor y bajo demanda por
  participante (`GET /<TOKEN>/backup`, con SHA256 en cabecera).

## Instalación (resumen)

1. VPS con Python 3.11+, Caddy y systemd. Crear usuario `evastate`.
2. `server.py` a `/opt/evastate/`, scripts a `/usr/local/sbin/`, unidades de
   `systemd/` a `/etc/systemd/system/`.
3. Copiar `config.example.env` a `/etc/evastate.env` y definir **todas** las
   variables (los defaults del código son neutros: sin configurar, el
   servidor responde por `state.example.com`).
4. Adaptar `caddy/Caddyfile.ejemplo` a tu dominio (apex + wildcard con TLS
   on-demand aprobado por el endpoint `/tls-check` del servidor).
5. Alta del primer participante:
   `sudo /opt/evastate/venv/bin/python /opt/evastate/participante.py alta <id> <tipo> "<Nombre>" <maquina>`
   (tipos: cowork|agente|servicio|humano; imprime el token UNA sola vez).
6. Cliente: `ejemplos/cliente_python.py` (config por entorno). Primer
   comando recomendado de cada sesión: `state_overview()`.

Detalle completo: `docs/OPERACION.md` · diseño y porqués: `docs/ARQUITECTURA.md`
· límites conocidos: `docs/ALCANCES-Y-LIMITES.md`.

## Pruebas

`tests/bateria.py` (solo stdlib): 7 puertas — humo, protocolo/conformidad,
identidad/seguridad, funcional, persistencia/respaldo, regresión y carga
ligera. Pensada para correr contra una **instancia sandbox** (mismo servidor,
puerto/base/participantes propios) desplegada por la misma ruta pública que
producción; contra producción solo se permite `--humo`. Config por entorno:
ver cabecera del archivo y `config.example.env`.

## Estructura

    server.py                  el servidor MCP (Python, SDK MCP v2, Starlette)
    scripts/eva-app-ctl        ayudante root: instala/gestiona apps dinámicas
    scripts/participante.py    altas/bajas/rotación de tokens (solo admin)
    scripts/eva-backup.py      respaldo diario del SQLite con verificación
    systemd/                   unidades: servicio, vigilante de spool, timer
    caddy/Caddyfile.ejemplo    reverse proxy + TLS on-demand con aprobación
    config.example.env         todas las variables de configuración
    docs/                      arquitectura, alcances y límites, operación
    ejemplos/                  cliente Python y app dinámica mínima
    tests/                     batería de pruebas (sandbox → producción)

## Reglas duras del canal

1. **Transporta coordinación, nunca inferencia.** El razonamiento de los
   agentes vive en sus propias máquinas; el canal solo coordina.
2. **Ni secretos ni datos biométricos.** Punteros a claves, nunca valores.
3. **Nada se borra**: bajas y cierres son lógicos; el historial es el activo.

## Límites conocidos (leer antes de exponerlo)

El token viaja en la URL (mitigar con TLS + rotación; si el riesgo crece,
migrar a header `Authorization`). No hay límite de tasa. Las apps dinámicas
ejecutan código arbitrario por diseño para participantes autorizados: la
defensa es el sandbox systemd y que las altas las haga solo el administrador.
Detalle: `docs/ALCANCES-Y-LIMITES.md`.
