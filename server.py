#!/usr/bin/env python3
"""Shared-state MCP server: identity-sealed messaging, facts, decisions,
subdomain/app deployment. All config via EVASTATE_* env vars."""
import json, os, re, sqlite3, sys, datetime, contextlib, contextvars, io, tarfile, hashlib, secrets
import time, logging, functools, inspect
from collections import deque

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, JSONResponse, HTMLResponse
from starlette.routing import Route

DB_PATH = os.environ.get("EVASTATE_DB", "/var/lib/evastate/state.db")
PARTICIPANTS_PATH = os.environ.get("EVASTATE_PARTICIPANTS", "/etc/evastate/participants.json")
DOMAIN = os.environ.get("EVASTATE_DOMAIN", "example.com")
PUBLIC_HOST = os.environ.get("EVASTATE_PUBLIC_HOST", f"state.{DOMAIN}")
SERVER_NAME = os.environ.get("EVASTATE_NAME", "estado-mcp")
PANEL_PATH = os.environ.get("EVASTATE_PANEL", os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html"))
SITES_DIR = os.environ.get("EVASTATE_SITES", "/var/www/sites")
MODO = os.environ.get("EVASTATE_MODO", "web")          # web | lan
SERVE_SITES = os.environ.get("EVASTATE_SERVE_SITES") == "1"
BIND = os.environ.get("EVASTATE_BIND", "127.0.0.1")
TLS_CERT = os.environ.get("EVASTATE_TLS_CERT", "")
TLS_KEY = os.environ.get("EVASTATE_TLS_KEY", "")
APPS_DIR = os.environ.get("EVASTATE_APPS", "/srv/apps")
CTL = "/usr/local/sbin/eva-app-ctl"
MAX_UPLOAD = 50 * 1024 * 1024  # 50 MB
NOMBRE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
RESERVADOS = {"www", "state", "mail", "smtp", "autodiscover", "_dmarc"}

CURRENT = contextvars.ContextVar("participante", default=None)

RATE_MAX = int(os.environ.get("EVASTATE_RATE_MAX", "240"))
RATE_WINDOW = int(os.environ.get("EVASTATE_RATE_WINDOW", "60"))
_RATE = {}

def _rate_ok(clave, maximo=None):
    """Ventana deslizante en memoria por identidad; el limite protege el canal,
    no sustituye la rotacion de un token comprometido."""
    ahora = time.monotonic()
    q = _RATE.setdefault(clave, deque())
    while q and ahora - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= (maximo or RATE_MAX):
        return False
    q.append(ahora)
    return True

def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()

def cargar_participantes():
    """El token ANTERIOR sigue abriendo la puerta mientras dure una rotacion.
    Sin esto, rotar deja al participante incomunicado en el instante exacto en que
    mas necesita el canal: para avisar de que no puede entrar. Se le deja entrar,
    se le marca, y se le grita en el saludo — pero no se le echa."""
    with open(PARTICIPANTS_PATH) as f:
        data = json.load(f)
    idx, viejos = {}, set()
    for pid, p in data.items():
        if not p.get("activo", True): continue
        h = p.get("token_sha256")
        if not h and p.get("token") and len(p["token"]) >= 24:
            h = _sha(p["token"])
        if h: idx[h] = pid
        hv = p.get("token_anterior_sha256")
        if hv and hv != h:
            idx[hv] = pid; viejos.add(hv)
    return data, idx, viejos

PARTICIPANTES, TOKEN_INDEX, TOKEN_VIEJOS = cargar_participantes()
CON_TOKEN_VIEJO = contextvars.ContextVar("token_viejo", default=False)

def _recargar_participantes():
    global PARTICIPANTES, TOKEN_INDEX, TOKEN_VIEJOS
    PARTICIPANTES, TOKEN_INDEX, TOKEN_VIEJOS = cargar_participantes()
    sembrar_participantes()

def _guardar_participantes():
    tmp = PARTICIPANTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(PARTICIPANTES, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o640)
    os.replace(tmp, PARTICIPANTS_PATH)

def ident():
    v = CURRENT.get()
    if not v:
        raise RuntimeError("sin identidad: la peticion no vino por una URL con token")
    return v

@contextlib.contextmanager
def db():
    """Conexion por operacion. El close() es obligatorio: `with sqlite3.connect()`
    solo hace commit/rollback y deja el descriptor abierto (fuga observada en
    produccion: 25 descriptores acumulados; con carga sostenida agota el limite)."""
    con = sqlite3.connect(DB_PATH, timeout=15)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=8000")
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT,
            data TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)""")
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_kind_key
                       ON items(kind, key) WHERE key IS NOT NULL""")
        con.execute("CREATE INDEX IF NOT EXISTS ix_kind ON items(kind)")
    sembrar_participantes()

def sembrar_participantes():
    """Espeja participants.json (SIN tokens) en la base, para consultas."""
    for pid, p in PARTICIPANTES.items():
        pub = {k: v for k, v in p.items() if k not in ("token", "token_sha256")}
        pub["id"] = pid
        _put("participant", pid, pub)

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def _put(kind, key, data):
    t = now()
    with db() as con:
        row = con.execute("SELECT id FROM items WHERE kind=? AND key=?", (kind, key)).fetchone()
        if row:
            con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                        (json.dumps(data, ensure_ascii=False), t, row["id"]))
            return {"accion": "actualizado", "id": row["id"], "kind": kind, "key": key}
        cur = con.execute("INSERT INTO items(kind,key,data,created,updated) VALUES(?,?,?,?,?)",
                          (kind, key, json.dumps(data, ensure_ascii=False), t, t))
        return {"accion": "creado", "id": cur.lastrowid, "kind": kind, "key": key}

def _append(kind, data):
    t = now()
    with db() as con:
        cur = con.execute("INSERT INTO items(kind,key,data,created,updated) VALUES(?,NULL,?,?,?)",
                          (kind, json.dumps(data, ensure_ascii=False), t, t))
        return {"accion": "creado", "id": cur.lastrowid, "kind": kind}

def _rows(kind, limit=300, order="ASC"):
    """limit=None trae TODO (en SQLite, LIMIT -1 no limita). Usarlo solo donde una
    respuesta recortada enganaria en silencio, no por comodidad."""
    with db() as con:
        cur = con.execute(
            f"SELECT id,key,data,created,updated FROM items WHERE kind=? ORDER BY id {order} LIMIT ?",
            (kind, -1 if limit is None else limit))
        out = []
        for r in cur.fetchall():
            d = json.loads(r["data"]); d["_id"] = r["id"]
            if r["key"]: d["_key"] = r["key"]
            d["_creado"] = r["created"]
            if r["updated"] != r["created"]: d["_actualizado"] = r["updated"]
            out.append(d)
        return out

def _get(kind, key):
    with db() as con:
        r = con.execute("SELECT id,data FROM items WHERE kind=? AND key=?", (kind, key)).fetchone()
    if not r: return None, None
    return r["id"], json.loads(r["data"])

# ───────────── HUELLA: quién sigue vivo, y quién ya lo tuvo delante ─────────────
# Esto existe por el desencuentro produccion/voicetf del 30-ago: los dos sostenian
# creencias opuestas y CONFIADAS sobre si habian hablado, y el canal no le ensenaba
# a ninguno el dato que las refutaba. Produccion leyo abandono donde habia trabajo;
# voicetf leyo, resolvio y no escribio, y el otro no tenia como distinguir "no la ha
# visto" de "la vio y no contesta". No se arregla pidiendo por regla que avisen: se
# arregla quitandole al canal la posibilidad de ocultar el dato.

_AUTOR = ("de", "dueno", "por", "autor", "quien", "solicitante", "cerrada_por")

def _tocar(pid):
    """Sella que esta identidad se conecto. Va en el unico punto por el que pasa
    TODA peticion autenticada: no hay manera de trabajar sin dejar huella, ni de
    olvidarse de dejarla."""
    try:
        _put("actividad", pid, {"id": pid, "ultima_conexion": now()})
    except Exception:
        pass    # la huella jamas puede tumbar una peticion

def _ultima_escritura():
    """Ultima vez que cada participante ESCRIBIO algo, del tipo que sea: mensaje,
    reserva de puerto, fecha, decision, hecho. Se DERIVA de la tabla, no de un
    contador aparte que haya que acordarse de subir — un contador se desincroniza
    cuando aparece un tipo nuevo, una derivacion no puede."""
    out = {}
    with db() as con:
        for r in con.execute("SELECT kind,data,created FROM items WHERE kind NOT IN "
                             "('participant','actividad') ORDER BY id DESC LIMIT 4000"):
            try: d = json.loads(r["data"])
            except Exception: continue
            for campo in _AUTOR:
                a = d.get(campo)
                if isinstance(a, str) and a in PARTICIPANTES:
                    out.setdefault(a, {"cuando": r["created"], "que": r["kind"]})
                    break
    return out

def _actividad(pid=None):
    """Quien esta vivo y quien contribuye. Dos columnas distintas a proposito:
    conectarse prueba que miras, escribir prueba que aportas, y confundirlas fue
    justo el error del 30-ago."""
    esc = _ultima_escritura()
    con_ = {r.get("id"): r.get("ultima_conexion") for r in _rows("actividad", 200)}
    ids = [pid] if pid else list(PARTICIPANTES)
    out = {}
    for i in ids:
        e = esc.get(i)
        out[i] = {"ultima_conexion": con_.get(i),
                  "ultima_escritura": (e or {}).get("cuando"),
                  "ultimo_escrito": (e or {}).get("que")}
    return out

def _entregar(pid, msgs):
    """Sella en el mensaje que su destinatario YA LO TUVO DELANTE, con hora.
    Se llama desde todo camino por el que el canal le ensena un mensaje a quien va
    dirigido. Convierte "no me respondio" de sospecha en dato, y separa lo que antes
    era indistinguible: no la ha abierto / la abrio y no contesta.
    Solo anade la primera vez; un camino de lectura que se olvide de llamarlo deja
    de sellar, pero NUNCA puede sellar de mas."""
    nuevos = [m for m in msgs if isinstance(m, dict) and m.get("_id")
              and m.get("para") == pid and m.get("de") != pid and not m.get("visto")]
    if not nuevos: return msgs
    t = now()
    try:
        with db() as con:
            for m in nuevos:
                r = con.execute("SELECT data FROM items WHERE id=?", (m["_id"],)).fetchone()
                if not r: continue
                d = json.loads(r["data"])
                if d.get("visto"): continue
                d["visto"] = t
                # No se toca `updated`: haber leido algo no es haberlo modificado.
                con.execute("UPDATE items SET data=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), m["_id"]))
                m["visto"] = t
    except Exception:
        pass
    return msgs

def _mis_solicitudes(me, msgs):
    """Mis solicitudes abiertas, con lo que antes habia que adivinar: si el otro ya
    la leyo, y que YO puedo cerrarla. Lo segundo va aqui porque voicetf afirmo que
    produccion no podia cerrar su propia solicitud —era falso— y produccion le creyo.
    Un permiso que no viaja junto al objeto se acaba inventando."""
    out = []
    for m in msgs:
        if m.get("de") != me or m.get("tipo") != "solicitud" or m.get("estado") != "abierta":
            continue
        ref = m.get("ref") or "?"
        e = {"ref": ref, "para": m.get("para"), "asunto": m.get("asunto"),
             "enviada": m.get("_creado")}
        if m.get("visto"):
            e["lectura"] = f"LA LEYO el {m['visto']} y no ha respondido"
        else:
            e["lectura"] = "AUN NO LA HA ABIERTO — no des por hecho que te ignora"
        e["puedes_cerrarla_tu"] = True
        e["como"] = f"sol_cerrar(ref='{ref}') — eres quien la abrio; no necesitas al otro"
        out.append(e)
    return out

def _next_ref(prefijo="SOL"):
    with db() as con:
        _, d = _get("seq", prefijo)
        n = (d or {}).get("n", 0) + 1
    _put("seq", prefijo, {"n": n})
    return f"{prefijo}-{n:03d}"

_REF_RE = re.compile(r"(?i)^(SOL|TEST|CART|FECHA)-(\d+)$")
def _norm_ref(s):
    """Forma canonica de una ref del canal (SOL-007 == SOL-7). None si no es ref."""
    m = _REF_RE.match((s or "").strip())
    return f"{m.group(1).upper()}-{int(m.group(2)):03d}" if m else None

def _jd(x): return json.dumps(x, ensure_ascii=False, indent=2)

def estacion(pid=None):
    """Maquina del participante, sellada por el servidor (no es un parametro)."""
    return (PARTICIPANTES.get(pid or ident(), {}).get("maquina") or "").strip()

def _solapa(a1, a2, b1, b2):
    return a1 <= b2 and b1 <= a2

def es_autoridad(pid):
    return bool(PARTICIPANTES.get(pid, {}).get("autoridad"))

init_db()

def _rechazar_parametros_desconocidos():
    """Sin esto el SDK IGNORA en silencio un argumento que no existe: quien llama
    cree que filtro y recibe todo. Paso de verdad (28-ago): un cowork leyo con un
    filtro inexistente, no filtro nada, y reporto que otro no habia respondido.
    Devuelve el nombre del modulo parcheado, o "" si el SDK cambio de sitio."""
    for mod in ("mcp.server.mcpserver.utilities.func_metadata",
                "mcp.server.fastmcp.utilities.func_metadata"):
        try:
            m = __import__(mod, fromlist=["ArgModelBase"])
            m.ArgModelBase.model_config["extra"] = "forbid"
            return mod
        except Exception:
            continue
    return ""

# Debe correr ANTES de registrar las tools: los modelos heredan la config al crearse.
SDK_ESTRICTO = _rechazar_parametros_desconocidos()
if not SDK_ESTRICTO:
    print("AVISO: no pude exigir parametros estrictos; el SDK cambio de estructura",
          file=sys.stderr)

mcp = MCPServer(SERVER_NAME)


def _marcar_errores_como_errores():
    """Un rechazo tiene que llegar marcado COMO rechazo, no solo escrito.

    El SDK pone is_error=True cuando la tool levanta una excepcion, pero deja
    is_error=False cuando devuelve una cadena normalmente. Todas nuestras
    validaciones hacian `return "ERROR: ..."`, asi que el cliente recibia el texto
    de un rechazo con el campo diciendo que todo fue bien.

    Lo encontro editorial el 2-sep-2026, y lo peor es a quien castigaba: un cliente
    que hace LO CORRECTO —fiarse de is_error— se tragaba el rechazo como si fuera
    un envio realizado. El que leia el texto a mano se salvaba por accidente. Es la
    misma familia que llevamos una semana cerrando, con el agravante de que aqui el
    campo existia y decia lo contrario de la verdad.

    Se arregla envolviendo el decorador UNA vez, no corrigiendo cincuenta returns:
    asi queda cubierta tambien cualquier tool que se escriba manana. La excepcion
    produce exactamente la misma forma que ya devuelven la tool inexistente y el
    parametro invalido, asi que los clientes no tienen que aprender nada nuevo.
    """
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except Exception:
        try:
            from mcp.server.fastmcp.exceptions import ToolError
        except Exception:
            ToolError = RuntimeError
    original = mcp.tool

    def tool_estricta(*a, **kw):
        decorar = original(*a, **kw)

        def envolver(fn):
            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def guardia(*args, **kwargs):
                    r = await fn(*args, **kwargs)
                    if isinstance(r, str) and r.lstrip().startswith("ERROR"):
                        raise ToolError(r)
                    return r
            else:
                @functools.wraps(fn)
                def guardia(*args, **kwargs):
                    r = fn(*args, **kwargs)
                    if isinstance(r, str) and r.lstrip().startswith("ERROR"):
                        raise ToolError(r)
                    return r
            return decorar(guardia)
        return envolver

    mcp.tool = tool_estricta
    return True


# Debe correr ANTES de registrar las tools, igual que el de parametros estrictos.
SDK_MARCA_ERRORES = _marcar_errores_como_errores()


# ───────────── RECURSOS COMPARTIDOS (la tarjeta, no el puerto) ─────────────
# El registro de puertos resolvia el conflicto equivocado. Produccion lo dijo en
# SOL-021 y nadie lo recogio: "compartir instancia no sirve: el problema es la
# descarga, no el puerto". En PC1 conviven tres puertos de produccion y un modelo
# de 10,5 GB de voicetf: los puertos ya no chocan y la VRAM si, sin que nada avise.
#
# Tres decisiones que conviene entender antes de tocar esto:
#  · AVISA, NO BLOQUEA. El canal no puede impedir que alguien use la tarjeta.
#    Fingir que si lo impide ensena a no consultarlo, que es peor que no tenerlo.
#  · LA MAQUINA LA SELLA EL SERVIDOR, igual que en los puertos.
#  · EL FALLO REAL ES OLVIDARSE DE SOLTAR, no tomar de mas. Por eso la reserva
#    declara cuanto va a durar y el estado ensena "tomado hace 6h, previsto 30min".
#    No se caduca sola: matar por reloj un lote legitimo que se alargo seria el
#    mismo error de fondo — decidir por alguien con menos informacion que el.

def _n(v, campo):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{campo} debe ser un numero entero")
    if n < 0:
        raise ValueError(f"{campo} no puede ser negativo")
    return n


@mcp.tool()
def recurso_medir(recurso: str, usado: int, fuente: str = "") -> str:
    """Reporta una lectura REAL de cuanto se esta usando de un recurso de tu
    estacion (por ejemplo, la salida de nvidia-smi).

    Existe por la critica de voicetf al diseno, que es la buena: un registro de
    DECLARACIONES se separa de la realidad igual que la convencion de mensajes que
    sustituye. Cambia la sintaxis, no la naturaleza. El lo demostro sobre si mismo:
    llevaba dias publicando '10,5 GB' —que era el tamano del fichero del modelo, no
    lo que ocupa en la tarjeta— y 'solo mientras genera', cuando en realidad no
    suelta la memoria hasta apagar el servidor. Nadie mintio: nadie midio.

    El canal no lee ninguna maquina; reporta cada estacion la suya. Lo unico que
    hace el canal es poner DECLARADO y MEDIDO uno al lado del otro, porque la
    divergencia entre los dos es la senal que hoy no existe."""
    me = ident(); maq = estacion()
    if not maq:
        return "ERROR: tu identidad no tiene estacion asignada."
    rid = recurso.strip().lower()
    if not _recurso_de(maq, rid):
        return f"ERROR: en {maq} no hay declarado ningun recurso '{rid}'."
    try:
        u = _n(usado, "usado")
    except ValueError as e:
        return f"ERROR: {e}"
    _put("medicion", f"{maq}:{rid}", {"maquina": maq, "recurso": rid, "usado": u,
                                      "medido_por": me, "cuando": now(),
                                      "fuente": fuente.strip() or "(sin indicar)"})
    return _jd({"accion": "medido", "recurso": rid, "maquina": maq, "usado": u})

@mcp.tool()
def recurso_declarar(id: str, capacidad: int, unidad: str = "MB", base: int = 0,
                     notas: str = "") -> str:
    """(SOLO autoridad) Declara que existe un recurso compartido en TU estacion.

    `capacidad` es la cifra FISICA, tal y como la da el hardware.
    `base` es lo que consume siempre algo que no pasa por este registro — el
    escritorio de Windows se come ~1.540 MiB de la 4060 Ti pase lo que pase.

    Las dos por separado y no una capacidad ya restada, porque si no se comparan
    peras con manzanas: la lectura de nvidia-smi es absoluta e incluye esa base,
    asi que restarla de la capacidad hacia saltar una divergencia falsa con la
    tarjeta en reposo. Y un rojo falso ensena a ignorar los rojos."""
    me = ident()
    if not es_autoridad(me):
        return "ERROR: declarar recursos es de la autoridad."
    maq = estacion()
    if not maq:
        return "ERROR: tu identidad no tiene estacion asignada."
    rid = id.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,31}", rid):
        return "ERROR: id invalido (minusculas, 2-32, letras/numeros/.-_). Ej: gpu-3090"
    try:
        cap = _n(capacidad, "capacidad")
    except ValueError as e:
        return f"ERROR: {e}"
    if cap <= 0:
        return "ERROR: la capacidad tiene que ser mayor que cero."
    try:
        base_n = _n(base, "base")
    except ValueError as e:
        return f"ERROR: {e}"
    if base_n >= cap:
        return f"ERROR: la base ({base_n}) no puede ser mayor o igual que la capacidad ({cap})."
    res = _put("recurso", f"{maq}:{rid}", {"maquina": maq, "recurso": rid,
               "capacidad": cap, "base": base_n, "unidad": unidad.strip() or "MB",
               "notas": notas.strip(), "declarado_por": me})
    return _jd({**res, "maquina": maq, "recurso": rid, "capacidad": cap,
                "base_fuera_del_registro": base_n, "repartible": cap - base_n})


def _recurso_de(maq, rid):
    _, r = _get("recurso", f"{maq}:{rid}")
    return r


def _tomas_vivas(maq, rid):
    return [t for t in _rows("toma", 500, order="DESC")
            if t.get("maquina") == maq and t.get("recurso") == rid
            and t.get("estado") == "tomado"]


@mcp.tool()
def recurso_tomar(recurso: str, cuanto: int, para: str, minutos: int = 0,
                  en_reposo: int = 0) -> str:
    """Anota que estas usando parte de un recurso compartido de tu estacion.

    `cuanto` es tu PICO PREVISTO, no lo que ocupas en reposo. Lo pidio asi voicetf
    con la medicion delante: su cerebro ocupa 9.736 MiB cargado y pica a 12.494 al
    generar. Si declarase el reposo, quien tomara los 14.800 restantes se quedaria
    sin memoria a mitad de su trabajo Y EL REGISTRO LE HABRIA DICHO QUE CABIA.
    Reservar por el pico desperdicia un poco de tarjeta; reservar por el reposo
    hace fallar a otro. `en_reposo` es opcional y solo informa.

    `para` evita que alguien mate tu proceso creyendo que sobra. `minutos` es lo
    que ESPERAS tardar: no caduca nada. Pedir de mas se avisa con nombres y cifras
    y se registra igual — el canal informa, no manda."""
    me = ident(); maq = estacion()
    if not maq:
        return "ERROR: tu identidad no tiene estacion asignada."
    rid = recurso.strip().lower()
    r = _recurso_de(maq, rid)
    if not r:
        hay = sorted(x["recurso"] for x in _rows("recurso", 200) if x.get("maquina") == maq)
        return (f"ERROR: en {maq} no hay declarado ningun recurso '{rid}'."
                + (f" Declarados: {', '.join(hay)}." if hay else
                   " No hay ninguno; pide a la autoridad que lo declare."))
    if not para.strip():
        return "ERROR: di para que lo usas; es lo que evita que alguien lo mate creyendo que sobra."
    try:
        cuanto_n = _n(cuanto, "cuanto")
        mins = _n(minutos, "minutos")
    except ValueError as e:
        return f"ERROR: {e}"
    if cuanto_n <= 0:
        return "ERROR: 'cuanto' tiene que ser mayor que cero."

    vivas = [t for t in _tomas_vivas(maq, rid) if t.get("dueno") != me]
    usado = sum(int(t.get("cuanto", 0)) for t in vivas)
    repartible = int(r["capacidad"]) - int(r.get("base", 0))
    libre = repartible - usado
    try:
        reposo_n = _n(en_reposo, "en_reposo")
    except ValueError as e:
        return f"ERROR: {e}"
    d = {"maquina": maq, "recurso": rid, "dueno": me, "cuanto": cuanto_n,
         "en_reposo": reposo_n, "para": para.strip(), "minutos_previstos": mins,
         "estado": "tomado", "desde": now()}
    res = _put("toma", f"{maq}:{rid}:{me}", d)
    salida = {**res, "recurso": rid, "maquina": maq, "tomas_tuyas": cuanto_n,
              "capacidad": r["capacidad"], "base_fuera_del_registro": r.get("base", 0),
              "repartible": repartible, "unidad": r.get("unidad", "MB"),
              "usado_por_otros": usado, "libre_antes_de_ti": libre,
              "quien_mas": [{"dueno": t["dueno"], "cuanto": t["cuanto"], "para": t.get("para")}
                            for t in vivas]}
    if cuanto_n > libre:
        salida["AVISO"] = (f"pides {cuanto_n} y solo quedaban {libre} de los {repartible} "
                           f"{r.get('unidad','MB')} repartibles. Queda registrado igual, "
                           "pero hablalo con quien lo tiene antes de arrancar: el canal avisa, "
                           "no impide.")
    return _jd(salida)


@mcp.tool()
def recurso_soltar(recurso: str) -> str:
    """Suelta lo que tenias tomado de un recurso de tu estacion."""
    me = ident(); maq = estacion()
    if not maq:
        return "ERROR: tu identidad no tiene estacion asignada."
    rid = recurso.strip().lower()
    i, d = _get("toma", f"{maq}:{rid}:{me}")
    if not d or d.get("estado") != "tomado":
        return f"ERROR: no tienes nada tomado de '{rid}' en {maq}."
    d["estado"] = "libre"; d["soltado"] = now()
    _put("toma", f"{maq}:{rid}:{me}", d)
    return _jd({"accion": "soltado", "recurso": rid, "maquina": maq,
                "tenias": d.get("cuanto")})


@mcp.tool()
def recurso_estado(recurso: str = "") -> str:
    """Quien tiene que, cuanto queda, y desde cuando. Sin `recurso`, todos los de
    tu estacion. Las reservas que pasan de lo previsto salen marcadas: casi siempre
    es alguien que se olvido de soltar, y verlo es lo unico que lo arregla."""
    maq = estacion()
    if not maq:
        return "ERROR: tu identidad no tiene estacion asignada."
    pedidos = [recurso.strip().lower()] if recurso.strip() else \
              sorted(x["recurso"] for x in _rows("recurso", 200) if x.get("maquina") == maq)
    if not pedidos:
        return _jd({"maquina": maq, "recursos": [],
                    "nota": "no hay ningun recurso declarado en esta estacion"})
    ahora = datetime.datetime.now(datetime.timezone.utc)
    salida = []
    for rid in pedidos:
        r = _recurso_de(maq, rid)
        if not r:
            salida.append({"recurso": rid, "error": "no declarado en esta estacion"}); continue
        tomas = []
        usado = 0
        for t in _tomas_vivas(maq, rid):
            usado += int(t.get("cuanto", 0))
            fila = {"dueno": t["dueno"], "cuanto": t["cuanto"], "para": t.get("para"),
                    "desde": t.get("desde")}
            try:
                mins = int((ahora - datetime.datetime.fromisoformat(t["desde"])).total_seconds() // 60)
                fila["lleva_minutos"] = mins
                prev = int(t.get("minutos_previstos") or 0)
                if prev and mins > prev * 2:
                    fila["SE_PASO"] = (
                        f"lleva {mins} min y preveia {prev}. Puede ser un lote que se alargo, "
                        "alguien que se olvido de soltarlo, o un proceso que retiene la memoria "
                        "POR DISENO hasta que se apaga (el cerebro local de voicetf es asi). "
                        "Preguntale; no des por libre la maquina.")
            except Exception:
                pass
            tomas.append(fila)
        base = int(r.get("base", 0))
        repartible = int(r["capacidad"]) - base
        libre = repartible - usado
        fila = {"recurso": rid, "capacidad": r["capacidad"],
                "base_fuera_del_registro": base, "repartible": repartible,
                "unidad": r.get("unidad", "MB"), "declarado": usado,
                "libre_segun_lo_declarado": libre, "notas": r.get("notas", ""),
                "tomado_por": tomas}
        _, med = _get("medicion", f"{maq}:{rid}")
        if med:
            fila["medido"] = med.get("usado")
            fila["medido_cuando"] = med.get("cuando")
            fila["medido_por"] = med.get("medido_por")
            try:
                edad = int((ahora - datetime.datetime.fromisoformat(med["cuando"])).total_seconds() // 60)
                fila["medido_hace_minutos"] = edad
                if edad > 120:
                    fila["MEDICION_VIEJA"] = (f"la ultima lectura real es de hace {edad} min: "
                                              "compara con cuidado o vuelve a medir.")
                # Solo importa que lo medido SUPERE lo contabilizado. Medir por debajo
                # es lo normal y esperado: las reservas se declaran por PICO y casi
                # nunca se esta en el pico. Marcar eso como divergencia seria un rojo
                # permanente, y un rojo que siempre esta encendido no informa de nada.
                elif int(med.get("usado", 0)) > (usado + base) + max(512, (usado + base) * 0.15):
                    exceso = int(med["usado"]) - (usado + base)
                    fila["MAS_USO_DEL_CONTABILIZADO"] = (
                        f"se estan usando {med['usado']} y solo hay {usado + base} contabilizados "
                        f"({usado} reservados + {base} de base): sobran {exceso} sin dueno. "
                        "O alguien esta usando la tarjeta sin anotarlo, o la base real es mayor "
                        "que la declarada. Lo segundo se arregla ajustando la base; lo primero "
                        "no se arregla solo.")
            except Exception:
                pass
        else:
            fila["medido"] = None
            fila["SIN_MEDIR"] = ("nadie ha reportado una lectura real de este recurso. Todo lo "
                                 "de arriba es lo que la gente CREE que ocupa, y eso se separa "
                                 "de la realidad sin que nadie lo note (recurso_medir).")
        # Un 'libre' negativo es EL dato que importa y pasaba como un numero mas.
        # Que el estado grave se lea igual que el normal es el fallo que llevamos
        # toda la semana cerrando; aqui lo cometi yo en la propia herramienta que
        # existe para hacerlo visible.
        if libre < 0:
            fila["SOBREPASADO"] = (
                f"hay {-libre} {r.get('unidad','MB')} comprometidos DE MAS sobre una capacidad de "
                f"{r['capacidad']}. Si todos usan a la vez lo que han anotado, alguien se va a "
                "quedar sin memoria a media faena. Hablalo antes de arrancar.")
        salida.append(fila)
    return _jd({"maquina": maq, "recursos": salida})

# ───────────── ROTACION DE TOKENS (nadie se queda fuera) ─────────────
def _confirmo_esta_rotacion(pid):
    """Confirmar UNA vez no vale para siempre. La confirmacion lleva el identificador
    de la rotacion que confirmaba; si no coincide con la abierta ahora, no cuenta."""
    p = PARTICIPANTES.get(pid) or {}
    if not p.get("token_anterior_sha256"):
        return True                       # no esta rotando: nada que confirmar
    _, r = _get("rotacion", pid)
    return bool(r) and r.get("rot_id") and r.get("rot_id") == p.get("rot_id")

@mcp.tool()
def token_confirmar() -> str:
    """Confirma que YA estas usando tu token nuevo. Llamalo despues de cambiarlo
    en tu configuracion. Solo cuenta si la llamada llega CON el token nuevo: por eso
    no puedes confirmar por error ni de buena fe sin haberlo probado."""
    me = ident()
    p = PARTICIPANTES.get(me) or {}
    if not p.get("token_anterior_sha256"):
        return _jd({"estado": "sin_rotacion_en_curso",
                    "nota": "No tienes ninguna rotacion pendiente. Nada que confirmar."})
    if CON_TOKEN_VIEJO.get():
        return ("ERROR: esta llamada ha llegado con tu token ANTIGUO, asi que no confirmo nada.\n"
                "Cambia el token en tu configuracion, reinicia tu cliente y vuelve a llamar.\n"
                "Que te deje entrar con el viejo es a proposito: para que puedas avisar si algo "
                "sale mal. No es senal de que ya lo hayas cambiado.")
    _put("rotacion", me, {"id": me, "confirmado": now(), "desde": p.get("rota_hasta"),
                          "rot_id": p.get("rot_id")})
    faltan = [q for q, v in PARTICIPANTES.items()
              if v.get("activo", True) and v.get("token_anterior_sha256")
              and not _confirmo_esta_rotacion(q)]
    return _jd({"estado": "confirmado", "id": me,
                "faltan_por_confirmar": faltan,
                "nota": ("Tu token viejo seguira valiendo hasta que el administrador cierre la "
                         "rotacion. No te quedaras fuera por haber confirmado.")})

@mcp.tool()
def rotacion_estado() -> str:
    """Quien ha confirmado ya su token nuevo y quien no. Solo la autoridad."""
    me = ident()
    if not es_autoridad(me):
        return "ERROR: solo la autoridad del canal consulta el estado de la rotacion."
    # Codigos emitidos y aun sin canjear, por participante. Un permiso vivo que no
    # se ve es un permiso que no se puede retirar.
    pendientes_cod = {}
    for r in _rows("rot_invitacion", 500, order="DESC"):
        if r.get("estado") == "emitida":
            pendientes_cod.setdefault(r.get("para"), r.get("emitida"))
    filas = []
    for q, v in PARTICIPANTES.items():
        if not v.get("activo", True): continue
        en_rot = bool(v.get("token_anterior_sha256"))
        _, r = _get("rotacion", q)
        vale = _confirmo_esta_rotacion(q)
        act = _actividad(q).get(q, {})
        filas.append({"id": q,
                      "en_rotacion": en_rot,
                      **({"codigo_emitido_sin_usar": pendientes_cod[q]}
                         if q in pendientes_cod else {}),
                      "confirmado": ((r or {}).get("confirmado") if vale else None),
                      **({"confirmacion_vieja_ignorada": (r or {}).get("confirmado")}
                         if (r and not vale and en_rot) else {}),
                      "ultima_conexion": act.get("ultima_conexion"),
                      "ultima_escritura": act.get("ultima_escritura")})
    pend = [f["id"] for f in filas if f["en_rotacion"] and not f["confirmado"]]
    sueltos = sorted(pendientes_cod)
    return _jd({"participantes": filas, "faltan": pend,
                "codigos_emitidos_sin_usar": sueltos,
                "listo_para_cerrar": not pend,
                "nota": ("Cerrar con: sudo participante.py cerrar-rotacion. Mientras alguien "
                         "figure en 'faltan', cerrar lo deja incomunicado. Si lleva dias sin "
                         "conectarse, mira ultima_conexion antes de dar por hecho que te ignora.")})

# ───────────── IDENTIDAD ─────────────
@mcp.tool()
def whoami() -> str:
    """Devuelve la identidad con la que este cliente escribe (la sella el servidor)."""
    pid = ident()
    p = {k: v for k, v in PARTICIPANTES[pid].items()
         if k not in ("token", "token_sha256", "token_anterior_sha256")}
    if PARTICIPANTES[pid].get("token_anterior_sha256"):
        p["rotacion"] = ("ESTAS USANDO EL TOKEN ANTIGUO" if CON_TOKEN_VIEJO.get()
                         else "usando el token nuevo; llama a token_confirmar() si aun no lo hiciste")
    return _jd({"id": pid, **p})

@mcp.tool()
def parametros(herramienta: str = "") -> str:
    """Los parametros que acepta cada herramienta, leidos del propio servidor.
    Sin argumento los lista todos. Lo que no este aqui se RECHAZA: el canal ya no
    ignora en silencio un parametro que no existe."""
    import asyncio
    try:
        tools = asyncio.run(mcp.list_tools())
    except RuntimeError:
        return "ERROR: no pude leer el catalogo en este momento."
    out = {}
    for t in tools:
        # el SDK lo expone como input_schema; por el cable viaja como inputSchema
        esq = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        props = esq.get("properties", {}) or {}
        req = set(esq.get("required", []) or [])
        out[t.name] = {
            "obligatorios": {k: v.get("type", "?") for k, v in props.items() if k in req},
            "opcionales": {k: v.get("type", "?") for k, v in props.items() if k not in req},
        }
    if herramienta:
        h = herramienta.strip()
        if h not in out:
            return f"ERROR: no existe la herramienta '{h}'. Llama a parametros() sin argumento para verlas."
        return _jd({h: out[h]})
    return _jd({"total": len(out), "nota": "un parametro que no figure aqui se rechaza", "herramientas": out})

@mcp.tool()
def participantes() -> str:
    """Lista de participantes registrados, con cuando se conecto y cuando escribio
    cada uno por ultima vez. Las dos columnas van AQUI para que nadie tenga que
    deducir de su bandeja si otro sigue activo: la bandeja solo ve mensajes
    dirigidos a uno, y por eso el 30-ago se leyo como abandono lo que era trabajo
    en registros que la bandeja no muestra."""
    act = _actividad()
    return _jd([{**p, **act.get(p.get("id"), {})} for p in _rows("participant")])

# ───────────── ARRANQUE ─────────────
@mcp.tool()
def state_overview() -> str:
    """Foto del estado compartido para MI identidad. Llamar al iniciar sesión."""
    me = ident()
    pend = [m for m in _rows("msg", 500, order="DESC")
            if m.get("para") in (me, "todos") and m.get("de") != me
            and m.get("estado") not in ("atendido", "respondida", "descartada")]
    _entregar(me, pend)
    _todos_msg = _rows("msg", 500, order="DESC")
    mias = _mis_solicitudes(me, _todos_msg)
    cart_pend = []
    for c in _rows("cartel", 300, order="DESC"):
        if c.get("estado") != "activo": continue
        if not (PARTICIPANTES.get(me) or {}).get("confirma_cartelera", True): continue
        if me in c.get("dirigido_a", []) and me not in c.get("confirmaciones", {}) \
                and c.get("requiere") in ("confirmacion", "respuesta"):
            cart_pend.append({"ref": c["ref"], "tipo": c["tipo"], "de": c["de"],
                              "asunto": c["asunto"], "requiere": c["requiere"]})
    por_aprobar = {}
    if es_autoridad(me):
        subs = [r["nombre"] for r in _rows("subdomain", 300) if r.get("estado") == "solicitado"]
        altas = [i.get("id") for i in _rows("invitacion", 200) if i.get("estado") == "solicitada"]
        if subs: por_aprobar["subdominios"] = subs
        if altas: por_aprobar["altas"] = altas
    maq = estacion(me)
    puertos = [{k: v for k, v in r.items() if k in ("puerto", "hasta", "servicio", "dueno", "notas")}
               for r in _rows("puerto", 500, order="DESC")
               if r.get("maquina") == maq and r.get("estado") == "ocupado"] if maq else []
    puertos.sort(key=lambda r: int(r["puerto"]))
    hoy_ = _hoy()
    f_todas = [r for r in _rows("fecha", 500, order="DESC") if r.get("estado") not in FINALES]
    def _resumen(r):
        return {"ref": r.get("_key"), "que": r.get("que"), "cuando": r["cuando"],
                "en_dias": _dias(hoy_, r["cuando"]), "estado": r.get("estado"),
                "dueno": r.get("dueno")}
    mis_fechas = sorted([_resumen(r) for r in f_todas if r.get("dueno") == me],
                        key=lambda x: x["cuando"])
    mis_prox = [f for f in mis_fechas if f["en_dias"] <= 14]
    del_resto = sorted([_resumen(r) for r in f_todas if r.get("dueno") != me
                        and 0 <= _dias(hoy_, r["cuando"]) <= 7], key=lambda x: x["cuando"])
    vencidas = [f for f in mis_fechas if f["en_dias"] < 0]
    bloqueadas = [f for f in mis_fechas if f["estado"] == "bloqueada"]

    return _jd({
        "yo": me,
        "mi_estacion": maq or "(sin asignar)",
        **({"FECHAS_MIAS_VENCIDAS": vencidas} if vencidas else {}),
        **({"mis_fechas_bloqueadas": bloqueadas} if bloqueadas else {}),
        **({"mis_fechas_14_dias": mis_prox} if mis_prox else {}),
        **({"del_resto_esta_semana": del_resto} if del_resto else {}),
        "puertos_de_mi_estacion": puertos,
        **({"por_aprobar": por_aprobar} if por_aprobar else {}),
        "cartelera_pendiente": cart_pend,
        "esperando_respuesta": mias,
        "actividad_de_todos": _actividad(),
        "mensajes_pendientes": pend,
        "decisiones_recientes": _rows("decision", 8, order="DESC"),
        "hechos": _rows("fact", 100),
        "infraestructura": _rows("infra", 100),
        "subdominios": _rows("subdomain", 200),
        "apps": _rows("app", 100),
        "nota": "Sin secretos ni datos personales. Coordinación, nunca inferencia (R-007).",
    })

# ───────────── MENSAJES (sustituyen a los archivos INTERCAMBIO) ─────────────
@mcp.tool()
def msg_send(para: str, asunto: str, cuerpo: str, tipo: str = "aviso", responde_a: str = "", ref: str = "") -> str:
    """Envía un mensaje. `para`: id de participante o 'todos' (por defecto úsalo:
    R-023 prohíbe filtrar por adelantado). `tipo`: aviso|solicitud|respuesta|bitacora|alerta.
    Si tipo=solicitud se asigna ref estable (SOL-NNN) y estado=abierta; se puede
    pasar `ref` explicita (SOL-N) para migrar solicitudes numeradas en los
    archivos puente — debe ser unica y el contador avanza para no chocar.
    Si tipo=respuesta, indicar `responde_a` con la ref de la solicitud."""
    me = ident()
    tipo = tipo.strip().lower()
    if tipo not in ("aviso", "solicitud", "respuesta", "bitacora", "alerta"):
        return "ERROR: tipo debe ser aviso|solicitud|respuesta|bitacora|alerta"
    para = para.strip().lower()
    if para != "todos" and para not in PARTICIPANTES:
        return f"ERROR: destinatario '{para}' no existe. Ver participantes()."
    d = {"de": me, "para": para, "asunto": asunto, "cuerpo": cuerpo, "tipo": tipo}
    if tipo == "solicitud":
        if ref:
            nref = _norm_ref(ref)
            if not nref:
                return "ERROR: la ref explicita debe tener forma SOL-N o TEST-N (ej. SOL-010). TEST-N es la serie de pruebas: no toca el contador SOL."
            ref = nref
            with db() as con:
                for r_ in con.execute("SELECT data FROM items WHERE kind='msg'").fetchall():
                    if _norm_ref(json.loads(r_["data"]).get("ref")) == ref:
                        return f"ERROR: la ref {ref} ya existe en el canal."
            if ref.startswith("SOL-"):
                n = int(ref.split("-")[1])
                _, sq = _get("seq", "SOL")
                if n > (sq or {}).get("n", 0):
                    _put("seq", "SOL", {"n": n})
            d["ref"] = ref
        else:
            d["ref"] = _next_ref("SOL")
        d["estado"] = "abierta"
    elif tipo == "respuesta":
        if not responde_a: return "ERROR: una respuesta necesita `responde_a` (ref SOL-N o texto libre, p.ej. 'archivo:2026-08-13')."
        ra = responde_a.strip()
        # solo se normaliza a mayusculas si es una ref del canal; el texto libre se respeta
        d["responde_a"] = _norm_ref(ra) or ra
        if str(d["responde_a"]).startswith("CART-"):
            cart = next((c for c in _rows("cartel", 300) if _norm_ref(c.get("ref")) == d["responde_a"]), None)
            if not cart: return f"ERROR: no existe el cartel {d['responde_a']}."
            if para != cart.get("de"):
                return (f"ERROR: las respuestas a la cartelera van EN PRIVADO a la autoridad emisora "
                        f"(para='{cart.get('de')}'), nunca a 'todos' ni a terceros.")
        d["estado"] = "pendiente"
    else:
        d["estado"] = "pendiente"
    res = _append("msg", d)
    if tipo == "respuesta" and str(d.get("responde_a", "")).startswith("CART-"):
        with db() as con:
            for r in con.execute("SELECT id,data FROM items WHERE kind='cartel'").fetchall():
                dc = json.loads(r["data"])
                if _norm_ref(dc.get("ref")) == d["responde_a"]:
                    dc.setdefault("confirmaciones", {})[me] = {"tipo": "respuesta", "msg_id": res["id"], "fecha": now()}
                    con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                                (json.dumps(dc, ensure_ascii=False), now(), r["id"]))
    if tipo == "respuesta" and _REF_RE.match(d["responde_a"]):
        # La solicitud pasa a respondida SALVO que responda quien la abrio: una
        # aclaracion propia es parte del hilo, no una respuesta. Cerrarla ahi
        # esconde trabajo pendiente que nadie ha atendido (D10, visto en SOL-015).
        with db() as con:
            for r in con.execute("SELECT id,data FROM items WHERE kind='msg'").fetchall():
                dd = json.loads(r["data"])
                if _norm_ref(dd.get("ref")) == d["responde_a"] and dd.get("tipo") == "solicitud":
                    if dd.get("de") == me:
                        continue
                    dd["estado"] = "respondida"
                    con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                                (json.dumps(dd, ensure_ascii=False), now(), r["id"]))
    return _jd({**res, **({"ref": d.get("ref")} if d.get("ref") else {})})

@mcp.tool()
def msg_inbox(incluir_atendidos: bool = False) -> str:
    """Qué hay abierto dirigido a MÍ (o a 'todos')."""
    me = ident()
    rows = _rows("msg", 500, order="DESC")
    out = [m for m in rows if m.get("para") in (me, "todos") and m.get("de") != me
           and (incluir_atendidos or m.get("estado") not in ("atendido", "respondida", "descartada"))]
    # los envios propios no son "bandeja": se consultan con search o msg_hilo (D1, 25-ago)
    return _jd(_entregar(me, out))

@mcp.tool()
def msg_desde(fecha_iso: str) -> str:
    """Todo lo escrito desde una fecha (ISO: 2026-08-23 o 2026-08-23T15:00:00+00:00).

    Una fecha ilegible se RECHAZA. Antes devolvia la lista vacia, y vacio se lee
    como "no ha pasado nada": quien preguntaba que habia desde el lunes con la
    fecha mal escrita recibia silencio y se lo creia. Es el mismo defecto que
    produccion reporto sobre esta herramienta el 28-ago —un filtro que no filtra—
    en otra forma, y en la misma funcion."""
    # SIN TOPE, y no es descuido. Con ORDER BY id ASC + LIMIT N, pasado el mensaje N
    # esto devuelve los N mas VIEJOS y descarta callado los recientes: justo lo que se
    # pide aqui. Detectado al cruzar el sandbox los 1000 mensajes (31-ago). Un recorte
    # silencioso en una herramienta de lectura es peor que un error: quien pregunta
    # "que ha pasado desde el lunes" se lleva un "nada" que se cree.
    f = (fecha_iso or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", f):
        return (f"ERROR: '{fecha_iso}' no es una fecha ISO. Usa 2026-08-23 o "
                "2026-08-23T15:00:00+00:00. Se rechaza en vez de devolver una lista "
                "vacia, porque vacio se lee como 'no ha pasado nada'.")
    try:
        datetime.date.fromisoformat(f[:10])
    except ValueError:
        return f"ERROR: '{fecha_iso[:10]}' no es una fecha valida del calendario."
    rows = _rows("msg", None, order="ASC")
    return _jd([m for m in rows if m["_creado"] >= f])

@mcp.tool()
def msg_hilo(ref: str) -> str:
    """El hilo completo de una ref (SOL-007): la solicitud y todas sus respuestas."""
    nref = _norm_ref(ref) or ref.strip()
    # Sin tope: un hilo incompleto no avisa de que le faltan piezas (ver msg_desde).
    rows = _rows("msg", None, order="ASC")
    def _eq(v): return bool(v) and (_norm_ref(v) or v.strip()) == nref
    hilo = _entregar(ident(), [m for m in rows
                               if _eq(m.get("ref")) or _eq(m.get("responde_a"))])
    if not hilo:
        # Una lista vacia no distingue "esa ref no existe" de "hilo sin mensajes",
        # y las dos se leen igual: como si no hubiera nada que ver. Sus vecinos ya
        # lo hacen explicito (puerto_quien dice encontrado:false, fact_get lo dice
        # con palabras); esta se habia quedado atras.
        return _jd({"ref": nref, "encontrado": False, "mensajes": [],
                    "nota": ("no hay ningun mensaje con esa referencia. Comprueba el numero "
                             "con search() o msg_inbox(): una ref mal escrita y un hilo vacio "
                             "se ven igual desde fuera, y no son lo mismo.")})
    return _jd(hilo)

@mcp.tool()
def msg_ack(id: int, nota: str = "") -> str:
    """Marca un mensaje dirigido a mí como atendido."""
    with db() as con:
        r = con.execute("SELECT data FROM items WHERE id=? AND kind='msg'", (id,)).fetchone()
        if not r: return f"ERROR: no existe el mensaje {id}."
        d = json.loads(r["data"])
        d["estado"] = "atendido"; d["atendido_por"] = ident()
        if nota: d["nota_cierre"] = nota
        con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                    (json.dumps(d, ensure_ascii=False), now(), id))
    return _jd({"accion": "atendido", "id": id})

@mcp.tool()
def sol_cerrar(ref: str, estado: str = "respondida", nota: str = "") -> str:
    """Cierra una solicitud: estado respondida|descartada."""
    if estado not in ("respondida", "descartada"):
        return "ERROR: estado debe ser respondida|descartada"
    ref = _norm_ref(ref) or ref.strip().upper()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='msg'").fetchall():
            d = json.loads(r["data"])
            if _norm_ref(d.get("ref")) == ref and d.get("tipo") == "solicitud":
                me = ident()
                if me not in (d.get("de"), d.get("para")) and d.get("para") != "todos":
                    return f"ERROR: {ref} es de {d.get('de')} para {d.get('para')}; solo los involucrados pueden cerrarla."
                d["estado"] = estado; d["cerrada_por"] = me
                if nota: d["nota_cierre"] = nota
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                # D2 (25-ago): al cerrar la solicitud, sus respuestas quedan atendidas
                atendidas = 0
                for r2 in con.execute("SELECT id,data FROM items WHERE kind='msg'").fetchall():
                    d2 = json.loads(r2["data"])
                    if d2.get("tipo") == "respuesta" and _norm_ref(d2.get("responde_a")) == ref \
                            and d2.get("estado") == "pendiente":
                        d2["estado"] = "atendido"; d2["atendido_por"] = f"cierre:{ref}"
                        con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                                    (json.dumps(d2, ensure_ascii=False), now(), r2["id"]))
                        atendidas += 1
                return _jd({"accion": estado, "ref": ref, "respuestas_atendidas": atendidas})
    return f"ERROR: no existe la solicitud {ref}."

# ───────────── CARTELERA (divulgación de la autoridad) e HISTORIAL ─────────────
@mcp.tool()
def cartel_publicar(tipo: str, asunto: str, cuerpo: str, requiere: str = "", formato_respuesta: str = "") -> str:
    """Publica en la cartelera (SOLO autoridad). tipo: regla|condicion|peticion|aviso.
    `requiere` (default por tipo): confirmacion — cada participante confirma que la
    integró a su regencia local; respuesta — respuesta PRIVADA a la autoridad en
    `formato_respuesta`; nada. Recibe ref estable CART-N."""
    me = ident()
    if not es_autoridad(me):
        return "ERROR: solo la autoridad publica en la cartelera; lo tuyo va por msg_send."
    tipo = tipo.strip().lower()
    if tipo not in ("regla", "condicion", "peticion", "aviso"):
        return "ERROR: tipo debe ser regla|condicion|peticion|aviso"
    req = (requiere or {"regla": "confirmacion", "condicion": "confirmacion",
                        "peticion": "respuesta", "aviso": "nada"}[tipo]).strip().lower()
    if req not in ("confirmacion", "respuesta", "nada"):
        return "ERROR: requiere debe ser confirmacion|respuesta|nada"
    if tipo == "peticion" and req == "respuesta" and not formato_respuesta:
        return "ERROR: una peticion de informacion declara `formato_respuesta` (el formato acordado)."
    ref = _next_ref("CART")
    # `confirma_cartelera: false` saca a un participante de la lista. Se puso para
    # Ricardo: la cartelera existe para propagar SU autoridad, y pedirle que confirme
    # lo que el mismo manda es ruido — ademas su nombre en `pendientes` hacia parecer
    # incompleta una cartelera que si lo estaba, y eso ensena a ignorar los pendientes.
    # Es un ATRIBUTO, no un caso especial por id: manana otro humano puede necesitarlo.
    dirigidos = [p for p, v in PARTICIPANTES.items()
                 if v.get("activo", True) and p != me
                 and v.get("confirma_cartelera", True)]
    d = {"de": me, "tipo": tipo, "asunto": asunto, "cuerpo": cuerpo, "ref": ref,
         "requiere": req, "formato_respuesta": formato_respuesta,
         "dirigido_a": dirigidos, "confirmaciones": {}, "estado": "activo"}
    res = _append("cartel", d)
    return _jd({**res, "ref": ref, "dirigido_a": dirigidos})

@mcp.tool()
def cartelera(incluir_cerrados: bool = False) -> str:
    """La cartelera de la autoridad vista por MI identidad: cada item trae mi_estado
    (confirmar, responder en privado, o al día). Prioridad máxima del canal."""
    me = ident()
    out = []
    for c in _rows("cartel", 300, order="DESC"):
        if c.get("estado") != "activo" and not incluir_cerrados: continue
        conf = c.get("confirmaciones", {})
        if me == c.get("de"): mi = "emisor"
        elif me not in c.get("dirigido_a", []): mi = "no dirigido"
        elif me in conf: mi = "al dia"
        elif c.get("requiere") == "confirmacion":
            mi = "PENDIENTE: confirmar integracion con cartel_confirmar"
        elif c.get("requiere") == "respuesta":
            mi = f"PENDIENTE: responder EN PRIVADO a {c.get('de')} (msg_send responde_a={c.get('ref')})"
        else: mi = "al dia"
        c.pop("confirmaciones", None)
        c["mi_estado"] = mi
        out.append(c)
    return _jd(out)

@mcp.tool()
def cartel_confirmar(ref: str, nota: str = "integrada en la regencia local") -> str:
    """Confirma que integraste a tu regencia local la regla/condición del cartel."""
    me = ident()
    ref = _norm_ref(ref) or ref.strip().upper()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='cartel'").fetchall():
            d = json.loads(r["data"])
            if _norm_ref(d.get("ref")) == ref:
                if me not in d.get("dirigido_a", []):
                    return f"ERROR: {ref} no esta dirigido a {me}."
                if d.get("requiere") == "respuesta":
                    return f"ERROR: {ref} pide respuesta privada a {d.get('de')} (msg_send responde_a={ref}), no confirmacion."
                d.setdefault("confirmaciones", {})[me] = {"tipo": "integrada", "fecha": now(), "nota": nota}
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return _jd({"accion": "confirmado", "ref": ref, "por": me})
    return f"ERROR: no existe el cartel {ref}."

@mcp.tool()
def cartel_estado(ref: str) -> str:
    """(SOLO autoridad) Matriz de un cartel: quién confirmó/respondió y quién falta."""
    me = ident()
    if not es_autoridad(me): return "ERROR: cartel_estado es de la autoridad."
    ref = _norm_ref(ref) or ref.strip().upper()
    for c in _rows("cartel", 300):
        if _norm_ref(c.get("ref")) == ref:
            conf = c.get("confirmaciones", {})
            pend = [p for p in c.get("dirigido_a", []) if p not in conf
                    and (PARTICIPANTES.get(p) or {}).get("confirma_cartelera", True)]
            return _jd({"ref": ref, "tipo": c.get("tipo"), "requiere": c.get("requiere"),
                        "estado": c.get("estado"), "confirmados": conf, "pendientes": pend})
    return f"ERROR: no existe el cartel {ref}."

@mcp.tool()
def cartel_cerrar(ref: str, nota: str = "") -> str:
    """(SOLO autoridad) Cierra un cartel: deja de exigir acción; queda en historial."""
    me = ident()
    if not es_autoridad(me): return "ERROR: cartel_cerrar es de la autoridad."
    ref = _norm_ref(ref) or ref.strip().upper()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='cartel'").fetchall():
            d = json.loads(r["data"])
            if _norm_ref(d.get("ref")) == ref:
                d["estado"] = "cerrado"; d["cerrado_por"] = me
                if nota: d["nota_cierre"] = nota
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return _jd({"accion": "cerrado", "ref": ref})
    return f"ERROR: no existe el cartel {ref}."

@mcp.tool()
def msg_historial(con_quien: str, limite: int = 200) -> str:
    """Historial completo de mensajes directos entre MI identidad y otro participante,
    cronológico. La MISMA vista para ambas partes: transparente y referenciable
    por _id y fecha, aunque pertenezcan a personas o cuentas distintas."""
    me = ident(); otro = con_quien.strip().lower()
    if otro not in PARTICIPANTES: return f"ERROR: '{otro}' no existe. Ver participantes()."
    rows = list(reversed(_rows("msg", 2000, order="DESC")))
    par = [m for m in rows if {m.get("de"), m.get("para")} == {me, otro}
           or (me == otro and m.get("de") == me and m.get("para") == me)]
    return _jd({"entre": sorted([me, otro]), "total": len(par), "mensajes": par[-limite:]})

# ───────────── FECHAS Y AVANCE (el scrum vive aqui) ─────────────
ESTADOS = ("pendiente", "en_curso", "bloqueada", "hecha", "cancelada")
FINALES = ("hecha", "cancelada")


def _fecha_ok(s):
    """Acepta 2026-09-01 y tambien 2026-09. Devuelve (iso, es_mes) o (None, None)."""
    s = (s or "").strip()
    try:
        if len(s) == 7:
            datetime.datetime.strptime(s, "%Y-%m")
            return s + "-01", True
        datetime.datetime.strptime(s[:10], "%Y-%m-%d")
        return s[:10], False
    except ValueError:
        return None, None


def _dias(a, b):
    da = datetime.date.fromisoformat(a)
    db_ = datetime.date.fromisoformat(b)
    return (db_ - da).days


def _hoy():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _choques(rango, recurso, excluir=None):
    """Fechas comprometidas del mismo recurso cuyo trabajo se solapa. Avisa, no
    bloquea: a veces el solape es legitimo y quien decide es la persona."""
    if not recurso:
        return []
    d1, d2 = rango
    fuera = []
    for r in _rows("fecha", 500, order="DESC"):
        if r.get("_key") == excluir or r.get("estado") in FINALES:
            continue
        if (r.get("recurso") or "").strip().lower() != recurso:
            continue
        if r.get("tipo") != "comprometida":
            continue
        o1 = r.get("desde") or r.get("cuando")
        o2 = r.get("cuando")
        if _solapa(_dias("2000-01-01", d1), _dias("2000-01-01", d2),
                   _dias("2000-01-01", o1), _dias("2000-01-01", o2)):
            fuera.append({"ref": r.get("_key"), "de": r.get("dueno"),
                          "que": r.get("que"), "cuando": o2})
    return fuera


@mcp.tool()
def fecha_comprometer(que: str, cuando: str, tipo: str = "comprometida",
                      desde: str = "", recurso: str = "", depende_de: str = "",
                      notas: str = "") -> str:
    """Registra una fecha que te comprometes a cumplir. Devuelve FECHA-N.
    `cuando`: 2026-09-15, o 2026-09 si solo sabes el mes. `tipo`: comprometida o
    aproximada. `desde`: cuando empiezas, si ocupa varios dias. `recurso`: lo que
    necesitas y otros podrian necesitar a la vez (GPU-3090, sala-de-grabacion, servidor-de-render...).
    El dueño lo pone el servidor desde tu identidad."""
    me = ident()
    que = que.strip()
    if not que:
        return "ERROR: di en una linea que te comprometes a hacer."
    tipo = (tipo or "comprometida").strip().lower()
    if tipo not in ("comprometida", "aproximada"):
        return "ERROR: tipo debe ser 'comprometida' o 'aproximada'."
    iso, es_mes = _fecha_ok(cuando)
    if not iso:
        return "ERROR: `cuando` debe ser 2026-09-15 o 2026-09 (solo el mes)."
    ini = iso
    if desde:
        ini, _ = _fecha_ok(desde)
        if not ini:
            return "ERROR: `desde` debe ser 2026-09-15 o 2026-09."
        if ini > iso:
            return "ERROR: `desde` es posterior a `cuando`."
    dep = (depende_de or "").strip()
    if dep:
        nd = _norm_ref(dep) or dep
        if nd.startswith("FECHA-") and not _get("fecha", nd)[1]:
            return f"ERROR: {nd} no existe. Consulta fecha_list()."
        dep = nd
    rec = (recurso or "").strip().lower()
    ref = _next_ref("FECHA")
    d = {"que": que, "cuando": iso, "solo_mes": es_mes, "desde": ini if ini != iso else "",
         "tipo": tipo, "recurso": rec, "depende_de": dep, "notas": notas.strip(),
         "dueno": me, "estado": "pendiente", "historial": []}
    _put("fecha", ref, d)
    ch = _choques((ini, iso), rec, excluir=ref)
    out = {"ref": ref, "que": que, "cuando": iso, "tipo": tipo, "estado": "pendiente"}
    if ch:
        out["AVISO_CHOQUE"] = (f"otros usan '{rec}' en esas fechas; no lo bloqueo, "
                               "pero hablalo antes")
        out["choca_con"] = ch
    return _jd(out)


@mcp.tool()
def fecha_mover(ref: str, nueva_fecha: str, motivo: str) -> str:
    """Cambia la fecha CONSERVANDO el historial de movimientos: sin esto, una
    fecha que se corre varias veces borra su propio rastro y nadie sabe cuanto
    se movio ni por que. El motivo es obligatorio, y no es para justificarse:
    es lo que permite ver patrones."""
    me = ident()
    nref = _norm_ref(ref) or (ref or "").strip().upper()
    _, d = _get("fecha", nref)
    if not d:
        return f"ERROR: {nref} no existe. Consulta fecha_list()."
    if d.get("dueno") != me and not es_autoridad(me):
        return f"ERROR: {nref} es de '{d.get('dueno')}'. Solo su dueño (o la autoridad) la mueve."
    if d.get("estado") in FINALES:
        return f"ERROR: {nref} ya esta {d.get('estado')}; no se mueve."
    if not (motivo or "").strip():
        return "ERROR: di por que se mueve. Es el dato que hace util el historial."
    iso, es_mes = _fecha_ok(nueva_fecha)
    if not iso:
        return "ERROR: la fecha debe ser 2026-09-15 o 2026-09."
    anterior = d["cuando"]
    if iso == anterior:
        return f"ERROR: {nref} ya estaba en {iso}."
    d.setdefault("historial", []).append(
        {"de": anterior, "a": iso, "motivo": motivo.strip(), "quien": me, "cuando": _hoy()})
    d["cuando"] = iso
    d["solo_mes"] = es_mes
    if d.get("desde") and d["desde"] > iso:
        d["desde"] = ""
    _put("fecha", nref, d)
    ch = _choques((d.get("desde") or iso, iso), d.get("recurso"), excluir=nref)
    out = {"ref": nref, "antes": anterior, "ahora": iso,
           "veces_movida": len(d["historial"]), "que": d.get("que")}
    if len(d["historial"]) >= 3:
        out["nota"] = f"esta fecha se ha movido {len(d['historial'])} veces; quiza el problema no es la fecha"
    if ch:
        out["AVISO_CHOQUE"] = f"otros usan '{d.get('recurso')}' en la fecha nueva"
        out["choca_con"] = ch
    return _jd(out)


@mcp.tool()
def fecha_estado(ref: str, estado: str, nota: str = "") -> str:
    """Avance de una fecha tuya: pendiente, en_curso, bloqueada, hecha o
    cancelada. 'bloqueada' es la mas util de todas: dice que no avanza y por que,
    antes de que llegue el dia."""
    me = ident()
    nref = _norm_ref(ref) or (ref or "").strip().upper()
    _, d = _get("fecha", nref)
    if not d:
        return f"ERROR: {nref} no existe. Consulta fecha_list()."
    if d.get("dueno") != me and not es_autoridad(me):
        return f"ERROR: {nref} es de '{d.get('dueno')}'."
    e = (estado or "").strip().lower().replace(" ", "_").replace("-", "_")
    if e not in ESTADOS:
        return f"ERROR: estado debe ser uno de {', '.join(ESTADOS)}."
    if e == "bloqueada" and not (nota or "").strip():
        return "ERROR: si esta bloqueada, di que la bloquea: es el dato que permite ayudarte."
    antes = d.get("estado")
    d["estado"] = e
    d.setdefault("avance", []).append(
        {"estado": e, "nota": nota.strip(), "quien": me, "cuando": _hoy()})
    if e in FINALES:
        d["cerrada_el"] = _hoy()
    _put("fecha", nref, d)
    out = {"ref": nref, "antes": antes, "ahora": e, "que": d.get("que")}
    if e in FINALES:
        colgadas = [r.get("_key") for r in _rows("fecha", 500)
                    if r.get("depende_de") == nref and r.get("estado") not in FINALES]
        if colgadas:
            out["dependian_de_esta"] = colgadas
    return _jd(out)


@mcp.tool()
def fecha_list(de: str = "", horizonte_dias: int = 0, incluir_cerradas: bool = False) -> str:
    """Fechas del proyecto. `de` filtra por dueño, `horizonte_dias` deja solo las
    que vencen en ese plazo. Por defecto no muestra las cerradas."""
    quien = (de or "").strip().lower()
    if quien and quien not in PARTICIPANTES:
        return f"ERROR: '{quien}' no es un participante. Ver participantes()."
    hoy = _hoy()
    out = []
    for r in _rows("fecha", 500, order="DESC"):
        if not incluir_cerradas and r.get("estado") in FINALES:
            continue
        if quien and r.get("dueno") != quien:
            continue
        dias = _dias(hoy, r["cuando"])
        if horizonte_dias and dias > int(horizonte_dias):
            continue
        out.append({"ref": r.get("_key"), "que": r.get("que"), "cuando": r["cuando"],
                    "en_dias": dias, "tipo": r.get("tipo"), "estado": r.get("estado"),
                    "dueno": r.get("dueno"),
                    **({"recurso": r["recurso"]} if r.get("recurso") else {}),
                    **({"depende_de": r["depende_de"]} if r.get("depende_de") else {}),
                    **({"movida_veces": len(r["historial"])} if r.get("historial") else {})})
    out.sort(key=lambda x: x["cuando"])
    vencidas = [x for x in out if x["en_dias"] < 0 and x["estado"] not in FINALES]
    return _jd({"total": len(out), **({"VENCIDAS": vencidas} if vencidas else {}), "fechas": out})


@mcp.tool()
def fecha_quien(recurso: str = "", cuando: str = "") -> str:
    """Quien tiene comprometido un recurso, o que hay comprometido para una fecha.
    Preguntalo ANTES de comprometer algo que necesite la misma maquina."""
    rec = (recurso or "").strip().lower()
    if not rec and not cuando:
        return "ERROR: di un recurso (GPU-3090) o una fecha (2026-09-15)."
    iso = None
    if cuando:
        iso, _ = _fecha_ok(cuando)
        if not iso:
            return "ERROR: la fecha debe ser 2026-09-15 o 2026-09."
    out = []
    for r in _rows("fecha", 500, order="DESC"):
        if r.get("estado") in FINALES:
            continue
        if rec and (r.get("recurso") or "").lower() != rec:
            continue
        if iso:
            ini = r.get("desde") or r["cuando"]
            if not (ini <= iso <= r["cuando"]):
                continue
        out.append({"ref": r.get("_key"), "dueno": r.get("dueno"), "que": r.get("que"),
                    "cuando": r["cuando"], "estado": r.get("estado"),
                    **({"desde": r["desde"]} if r.get("desde") else {}),
                    **({"recurso": r["recurso"]} if r.get("recurso") else {})})
    out.sort(key=lambda x: x["cuando"])
    return _jd({"encontrado": bool(out), "criterio": {"recurso": rec or None, "cuando": iso},
                "total": len(out), "fechas": out})


@mcp.tool()
def fecha_hilo(ref: str) -> str:
    """Todo lo que le ha pasado a una fecha: movimientos con su motivo y avance."""
    nref = _norm_ref(ref) or (ref or "").strip().upper()
    _, d = _get("fecha", nref)
    if not d:
        return f"ERROR: {nref} no existe."
    return _jd({"ref": nref, "que": d.get("que"), "dueno": d.get("dueno"),
                "cuando": d.get("cuando"), "tipo": d.get("tipo"), "estado": d.get("estado"),
                "recurso": d.get("recurso") or None, "depende_de": d.get("depende_de") or None,
                "notas": d.get("notas") or None,
                "movimientos": d.get("historial", []), "avance": d.get("avance", [])})

# ───────────── ALTAS REMOTAS (invitación + aprobación de la autoridad) ─────────────

def _texto_invitacion(codigo, id_sugerido=""):
    """Guion autocontenido para el cowork nuevo: sirve en cualquier maquina,
    usuario y proyecto sin que la autoridad tenga que editar nada."""
    base = f"https://{PUBLIC_HOST}"
    linea_id = (f'ID = "{id_sugerido}"' if id_sugerido
                else 'ID = ""   # <-- pon aqui tu id: minusculas, corto (ej. "marketing", "agente-01")')
    aviso_id = (f"Tu id asignado es `{id_sugerido}` y es inmutable." if id_sugerido
                else "Elige un id corto en minusculas y ponlo en la linea ID del script; sera inmutable.")
    return (
"Te vas a unir a **state**, el canal de coordinacion entre coworks (" + base + ").\n"
"Da identidad verificable a cada participante: mensajes con referencia estable,\n"
"solicitudes con estado, decisiones, hechos canonicos y una cartelera de reglas.\n"
"El servidor sella tu identidad: nadie puede escribir como tu, ni tu como otro.\n"
"\n" + aviso_id + "\n"
"\n"
"PASO 1 - Donde vive tu clave\n"
"\n"
"Tu token va en una carpeta llamada `state`, creada en el directorio raiz de\n"
"mayor prioridad al que tengas acceso (el nivel mas alto de tu arbol de trabajo)\n"
"y que NO este dentro de ningun repositorio git. El script de abajo la crea sola\n"
"en el sitio correcto y le deja un .gitignore que la excluye por completo, por si\n"
"algun dia queda dentro de un repo.\n"
"\n"
"Tu token NUNCA se sube a un repositorio, ni se pega en un chat, ni se comparte.\n"
"\n"
"PASO 2 - Ejecuta este script (Python 3, cualquier sistema operativo)\n"
"\n"
"Genera tu token, lo guarda y pide tu alta. El servidor solo recibe su hash: el\n"
"valor en claro existe unicamente en tu maquina.\n"
"\n"
"-----------------------------------------------------------------------------\n"
"import json, os, secrets, socket, sys, urllib.request, urllib.error\n"
"from pathlib import Path\n"
"\n"
'CODIGO = "' + codigo + '"\n'
+ linea_id + "\n"
'TIPO = "cowork"     # cowork | agente | servicio | humano\n'
'NOMBRE = ""         # nombre legible; vacio = usa el id\n'
'BASE = "' + base + '"\n'
"\n"
'if not ID.strip(): sys.exit("Define ID antes de ejecutar.")\n'
"\n"
"def raiz_para_state():\n"
'    """Directorio mas alto accesible que NO este dentro de un repo git."""\n'
"    aqui = Path.cwd().resolve(); hogar = Path.home().resolve()\n"
"    def en_repo(p): return any((a / '.git').exists() for a in [p, *p.parents])\n"
"    repo = next((p for p in [aqui, *aqui.parents] if (p / '.git').exists()), None)\n"
"    candidatos = []\n"
"    if repo is not None and repo.parent != repo: candidatos.append(repo.parent)\n"
"    alto = aqui\n"
"    for p in [aqui, *aqui.parents]:\n"
"        if p == p.parent: break\n"
"        if os.access(p, os.W_OK): alto = p\n"
"    candidatos += [alto, hogar]\n"
"    for c in candidatos:\n"
"        if c and os.access(c, os.W_OK) and not en_repo(c): return c\n"
"    return hogar\n"
"\n"
"carpeta = raiz_para_state() / 'state'\n"
"carpeta.mkdir(parents=True, exist_ok=True)\n"
"(carpeta / '.gitignore').write_text('*\\n', encoding='utf-8')   # nunca al repo\n"
"\n"
"destino = carpeta / (ID.strip().lower() + '.token')\n"
"if destino.exists():\n"
"    token = destino.read_text(encoding='utf-8').strip()\n"
"    print('Reutilizo el token que ya estaba en', destino)\n"
"else:\n"
"    token = secrets.token_urlsafe(36)\n"
"    destino.write_text(token, encoding='utf-8')\n"
"    try: os.chmod(destino, 0o600)\n"
"    except Exception: pass\n"
"    print('Token creado y guardado en', destino)\n"
"\n"
"datos = {'codigo': CODIGO, 'id': ID.strip().lower(), 'tipo': TIPO,\n"
"         'nombre': NOMBRE or ID, 'maquina': socket.gethostname() or 'sin-nombre',\n"
"         'token_propuesto': token}\n"
"req = urllib.request.Request(BASE + '/registro', json.dumps(datos).encode(),\n"
"    {'Content-Type': 'application/json', 'User-Agent': 'state-cliente/1.0'}, method='POST')\n"
"try:\n"
"    print(urllib.request.urlopen(req, timeout=30).read().decode())\n"
"    print('Solicitud enviada. Espera la aprobacion de la autoridad: hasta entonces tu token no funciona.')\n"
"except urllib.error.HTTPError as e:\n"
"    print('No se pudo registrar:', e.code, e.read().decode()[:300])\n"
"-----------------------------------------------------------------------------\n"
"\n"
"Apunta la RUTA que imprime el script: es donde vive tu token y la necesitaras\n"
"en cada sesion. Manda siempre un User-Agent propio en tus peticiones (algunos\n"
"proxys rechazan el de las librerias por defecto).\n"
"\n"
"PASO 3 - Cuando la autoridad apruebe, comprueba tu identidad\n"
"\n"
"    POST " + base + "/<TU_TOKEN>/mcp\n"
'    {"jsonrpc":"2.0","id":1,"method":"tools/call",\n'
'     "params":{"name":"whoami","arguments":{}}}\n'
"\n"
"Con ese mismo token puedes usar la consola web, sin cuenta aparte:\n"
"    " + base + "/panel\n"
"\n"
"PASO 4 - Protocolo de apertura, obligatorio en CADA sesion\n"
"\n"
"1. state_overview() - tu bandeja, la cartelera pendiente, tus solicitudes sin\n"
"   responder, decisiones y hechos.\n"
"2. Baja el respaldo del dia: GET /<TU_TOKEN>/backup, verifica que el SHA-256 del\n"
"   cuerpo coincide con la cabecera X-Backup-Sha256 y guardalo junto a tu token.\n"
"\n"
"Reglas que debes conocer desde el primer dia\n"
"\n"
"- La CARTELERA es la fuente de las reglas: las publica la autoridad. Confirma\n"
"  cada una con cartel_confirmar(ref) cuando la integres a tu regencia local.\n"
"  Las peticiones de la autoridad se responden EN PRIVADO a quien las emitio,\n"
"  nunca a 'todos'.\n"
"- El canal transporta coordinacion, NUNCA inferencia. Ni secretos ni datos\n"
"  biometricos: punteros (rutas), jamas valores.\n"
"- Prueba solo con la serie TEST-N (msg_send(..., ref='TEST-1')), nunca sobre la\n"
"  serie SOL real.\n"
"- No hagas msg_ack de avisos dirigidos a 'todos': el estado es global y se lo\n"
"  ocultarias al resto.\n"
"- Para publicar una demo o una app: subdomain_claim('nombre') deja el subdominio\n"
"  SOLICITADO; hasta que la autoridad lo apruebe no puedes desplegar ni se emite\n"
"  certificado. Detalles con deploy_info().\n"
"- Si el canal no responde: avisalo en voz alta y sigue en tu archivo local. Un\n"
"  canal que falla en silencio es peor que no tener canal.\n"
"\n"
"Empieza por whoami() y state_overview().\n")

@mcp.tool()
def alta_invitar(nota: str = "", id_sugerido: str = "") -> str:
    """(SOLO autoridad) Emite una invitación de UN SOLO USO (caduca en 7 días) para
    que un cliente nuevo solicite su alta vía POST /registro. El código se muestra
    UNA vez: entrégalo al candidato por un canal privado."""
    me = ident()
    if not es_autoridad(me): return "ERROR: alta_invitar es de la autoridad."
    sug = id_sugerido.strip().lower()
    if sug and not re.fullmatch(r"[a-z][a-z0-9-]{1,19}", sug):
        return "ERROR: id_sugerido invalido: minusculas, [a-z][a-z0-9-]{1,19}."
    if sug and sug in PARTICIPANTES:
        return f"ERROR: el id '{sug}' ya existe."
    codigo = secrets.token_urlsafe(18)
    _append("invitacion", {"codigo_sha256": _sha(codigo), "emitida_por": me, "id_sugerido": sug,
                           "estado": "emitida", "nota": nota, "emitida": now()})
    return _jd({"codigo": codigo,
                "aviso": "el codigo se muestra UNA sola vez y caduca en 7 dias",
                "texto_para_el_cowork": _texto_invitacion(codigo, sug)})

# ───────────── SEGUNDO SECRETO PARA EL CICLO DE VIDA DE CREDENCIALES ─────────────
# Poner alta/baja/rotacion en la consola cambia lo que consigue quien robe el token
# de la autoridad: antes leer y escribir; ahora ACUNAR Y REVOCAR IDENTIDADES. Esa
# escalada se paga con un segundo factor.
#
# La frase NO se puede fijar por la API, a proposito: un secreto que protege una
# operacion no puede establecerse con la credencial que esa operacion protege. Se
# pone a mano en /etc/evastate.env, y solo su SHA-256:
#
#     EVASTATE_FRASE_SHA256=$(printf %s 'tu frase' | sha256sum | cut -d" " -f1)
#
# Si no esta definida, las operaciones protegidas se NIEGAN. No se degradan a
# "solo autoridad" en silencio: una proteccion que desaparece sola cuando falta su
# configuracion es peor que no tenerla, porque nadie se entera.
FRASE_SHA = (os.environ.get("EVASTATE_FRASE_SHA256") or "").strip().lower()

def _frase_ok(frase):
    """Devuelve (True, '') o (False, motivo). Comparacion en tiempo constante."""
    if not FRASE_SHA:
        return False, ("ERROR: esta instalacion no tiene frase de seguridad configurada, asi que "
                       "las operaciones sobre credenciales estan cerradas.\n"
                       "Definela en /etc/evastate.env y reinicia el servicio:\n"
                       "  EVASTATE_FRASE_SHA256=$(printf %s 'tu frase' | sha256sum | cut -d' ' -f1)\n"
                       "Se pone a mano y por SSH a proposito: si se pudiera fijar desde aqui, no "
                       "protegeria de nada.")
    if not frase:
        return False, ("ERROR: esta operacion crea o revoca credenciales y necesita la frase de "
                       "seguridad. Pasala en el parametro `frase`.")
    if not secrets.compare_digest(_sha(frase), FRASE_SHA):
        _append("intento_frase", {"quien": ident(), "cuando": now()})
        return False, ("ERROR: frase incorrecta. El intento queda registrado. Si no has sido tu, "
                       "alguien tiene tu token: rota YA y revisa intentos_frase().")
    return True, ""

@mcp.tool()
def intentos_frase(limite: int = 20) -> str:
    """(SOLO autoridad) Intentos fallidos con la frase de seguridad. Un fallo que no
    reconozcas significa que alguien tiene un token de autoridad que no deberia."""
    if not es_autoridad(ident()):
        return "ERROR: intentos_frase es de la autoridad."
    return _jd(_rows("intento_frase", max(1, min(int(limite), 200)), order="DESC"))

@mcp.tool()
def participante_baja(id: str, frase: str = "", motivo: str = "") -> str:
    """(AUTORIDAD + frase) Da de baja logica a un participante: su token deja de
    abrir y su historial se conserva. Antes esto solo se podia hacer por SSH."""
    me = ident()
    if not es_autoridad(me): return "ERROR: participante_baja es de la autoridad."
    ok, err = _frase_ok(frase)
    if not ok: return err
    pid = id.strip().lower()
    if pid == me: return "ERROR: no puedes darte de baja a ti mismo desde el canal."
    p = PARTICIPANTES.get(pid)
    if not p: return f"ERROR: '{pid}' no existe."
    if not p.get("activo", True): return f"ERROR: '{pid}' ya estaba de baja."
    p["activo"] = False; p["baja_por"] = me; p["baja_fecha"] = now()
    if motivo: p["baja_motivo"] = motivo
    _guardar_participantes(); _recargar_participantes()
    _append("decision", {"de": me, "titulo": f"baja de '{pid}'", "proyecto": "state",
                         "decision": f"'{pid}' dado de baja desde la consola.",
                         "motivo": motivo or "(sin motivo declarado)", "fecha": now()})
    return _jd({"accion": "baja", "id": pid,
                "aviso": "queda registrado como decision del canal; su token ya no abre"})

@mcp.tool()
def participante_cartelera(id: str, confirma: bool, frase: str = "") -> str:
    """(AUTORIDAD + frase) Marca si un participante confirma carteles. Ponlo en false
    para servicios y para la autoridad de la que emanan las reglas."""
    me = ident()
    if not es_autoridad(me): return "ERROR: participante_cartelera es de la autoridad."
    ok, err = _frase_ok(frase)
    if not ok: return err
    pid = id.strip().lower()
    if pid not in PARTICIPANTES: return f"ERROR: '{pid}' no existe."
    PARTICIPANTES[pid]["confirma_cartelera"] = bool(confirma)
    _guardar_participantes(); _recargar_participantes()
    return _jd({"accion": "actualizado", "id": pid, "confirma_cartelera": bool(confirma)})

@mcp.tool()
def rotacion_anular(id: str, frase: str = "") -> str:
    """(AUTORIDAD + frase) Anula el codigo de rotacion vivo de un participante para
    poder emitir otro. Existe porque el codigo se muestra UNA vez: perderlo dejaba
    bloqueado a ese participante hasta que caducara. Lo descubri perdiendo uno.
    Anular NO toca ningun token: solo invalida un permiso que aun no se ha usado."""
    me = ident()
    if not es_autoridad(me): return "ERROR: rotacion_anular es de la autoridad."
    ok, err = _frase_ok(frase)
    if not ok: return err
    pid = id.strip().lower()
    n = 0
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='rot_invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("para") == pid and d.get("estado") == "emitida":
                d["estado"] = "anulada"; d["anulada_por"] = me; d["anulada"] = now()
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                n += 1
    if not n: return f"ERROR: '{pid}' no tiene ningun codigo vivo que anular."
    return _jd({"accion": "anulados", "codigos": n, "para": pid,
                "nota": "ningun token se ha tocado; ya puedes emitir otro"})

@mcp.tool()
def rotacion_cerrar(id: str = "", frase: str = "", forzar: bool = False) -> str:
    """(AUTORIDAD + frase) Retira los tokens antiguos. Sin `id`, cierra todas las
    rotaciones abiertas. Se NIEGA mientras alguien no haya confirmado: cerrar sobre
    quien no ha confirmado lo deja fuera del canal, y sin canal no puede avisar de
    que esta fuera. `forzar` existe, pero hay que escribirlo."""
    me = ident()
    if not es_autoridad(me): return "ERROR: rotacion_cerrar es de la autoridad."
    ok, err = _frase_ok(frase)
    if not ok: return err
    pid = id.strip().lower()
    objetivo = ([pid] if pid else
                [q for q, v in PARTICIPANTES.items() if v.get("token_anterior_sha256")])
    if not objetivo: return "ERROR: no hay ninguna rotacion abierta."
    faltan = []
    for q in objetivo:
        v = PARTICIPANTES.get(q) or {}
        if not v.get("token_anterior_sha256"): continue
        if not _confirmo_esta_rotacion(q): faltan.append(q)
    if faltan and not forzar:
        det = {q: (_actividad(q).get(q) or {}).get("ultima_conexion") for q in faltan}
        return _jd({"error": "NO cierro nada: estos no han confirmado su token nuevo",
                    "faltan": faltan, "ultima_conexion_de_cada_uno": det,
                    "antes_de_forzar": ("mira la ultima conexion: quien lleva dias sin aparecer no "
                                        "te ignora, no ha vuelto. Forzar lo deja fuera y sin forma "
                                        "de avisarte."),
                    "como_forzar": "rotacion_cerrar(..., forzar=True) — deliberado, no por descuido"})
    cerrados = []
    for q in objetivo:
        v = PARTICIPANTES.get(q) or {}
        if not v.pop("token_anterior_sha256", None): continue
        v.pop("rota_hasta", None); v.pop("rot_id", None); cerrados.append(q)
    _guardar_participantes(); _recargar_participantes()
    return _jd({"accion": "rotacion cerrada", "participantes": cerrados,
                "forzado": bool(forzar and faltan),
                **({"quedaron_fuera": faltan} if (forzar and faltan) else {})})

@mcp.tool()
def rotacion_invitar(id: str, dias: int = 7, frase: str = "") -> str:
    """(SOLO autoridad) Emite un codigo de UN SOLO USO para que un participante
    cambie su token. El codigo NO es la credencial: es el permiso para proponer una.
    El propio cliente genera su token nuevo y lo envia por POST /rotacion; el
    servidor no emite ni transmite tokens jamas. Por eso el codigo se puede pasar
    por una via debil sin comprometer nada, y ningun token acaba en un fichero
    compartido ni en una URL — que es como acabaron en los registros el 2-sep."""
    me = ident()
    if not es_autoridad(me): return "ERROR: rotacion_invitar es de la autoridad."
    ok, err = _frase_ok(frase)
    if not ok: return err
    pid = id.strip().lower()
    p = PARTICIPANTES.get(pid)
    if not p or not p.get("activo", True): return f"ERROR: '{pid}' no existe o no esta activo."
    if p.get("token_anterior_sha256") and _confirmo_esta_rotacion(pid):
        return (f"ERROR: '{pid}' ya rota y YA HA CONFIRMADO su token nuevo. Cierra esa "
                "rotacion antes de abrir otra, o quedarian tres tokens vivos.")
    # Si rota pero AUN NO ha confirmado, se permite emitir otro codigo: el token
    # nuevo no lo esta usando nadie todavia. Es la unica salida cuando ese token se
    # pierde entre el canje y el disco, que es justo lo que paso el 2-sep.
    for r in _rows("rot_invitacion", 500, order="DESC"):
        if r.get("para") == pid and r.get("estado") == "emitida":
            return (f"ERROR: ya hay un codigo vivo para '{pid}'. Dos codigos a la vez es una "
                    "copia mas del permiso que puede perderse.\n"
                    f"Si lo has perdido: rotacion_anular('{pid}') y vuelve a emitir.")
    codigo = secrets.token_urlsafe(18)
    _append("rot_invitacion", {"codigo_sha256": _sha(codigo), "para": pid, "estado": "emitida",
                               "emitida_por": me, "emitida": now(), "dias": int(dias)})
    return _jd({"codigo": codigo, "para": pid,
                "aviso": f"se muestra UNA vez y caduca en {int(dias)} dias",
                "instrucciones": _texto_rotacion(codigo, pid)})

def _texto_rotacion(codigo, pid):
    return (
        f"Cambio de token de '{pid}'. Genera TU el nuevo; el canal no te lo manda.\n\n"
        "python -c \"import secrets,json,urllib.request;"
        "t=secrets.token_urlsafe(36);"
        f"d=json.dumps({{'codigo':'{codigo}','token_propuesto':t}}).encode();"
        "r=urllib.request.Request('https://" + PUBLIC_HOST + "/rotacion',d,"
        "{'Content-Type':'application/json','User-Agent':'eva-rotacion/1.0'});"
        "print(urllib.request.urlopen(r,timeout=30).read().decode());"
        "open(RUTA,'w').write(t)\"\n\n"
        "Cambia RUTA por el fichero donde lees tu token (esta en infra_list como "
        f"token-{pid}). Tu token ANTIGUO sigue valiendo hasta que confirmes: reinicia "
        "tu cliente y llama a token_confirmar().")

async def rotacion_post(request):
    """Canje del codigo de rotacion. El cliente propone su token; aqui no se emite
    ninguno. El antiguo NO se retira: pasa a token_anterior_sha256 y sigue abriendo
    la puerta hasta que su dueno confirme con el nuevo."""
    try:
        body = json.loads(await request.body())
    except Exception:
        return JSONResponse({"error": "JSON invalido"}, status_code=400)
    codigo = str(body.get("codigo", "")); tok = str(body.get("token_propuesto", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", tok):
        return JSONResponse({"error": "token_propuesto: 32-128 caracteres url-safe, generado por ti"},
                            status_code=400)
    h_nuevo = _sha(tok)
    if h_nuevo in TOKEN_INDEX:
        return JSONResponse({"error": "ese token ya esta en uso; genera otro"}, status_code=409)
    h = _sha(codigo)
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='rot_invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("codigo_sha256") != h or d.get("estado") != "emitida":
                continue
            try:
                ed = datetime.datetime.fromisoformat(d.get("emitida"))
                caducado = (datetime.datetime.now(datetime.timezone.utc) - ed).days >= int(d.get("dias", 7))
            except Exception:
                caducado = False
            if caducado:
                d["estado"] = "caducada"
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return JSONResponse({"error": "codigo caducado; pide otro a la autoridad"}, status_code=410)
            pid = d.get("para")
            p = PARTICIPANTES.get(pid)
            if not p or not p.get("activo", True):
                return JSONResponse({"error": "participante no activo"}, status_code=409)
            # El "anterior" es SIEMPRE el ultimo token que se sabe que funciona. Si ya
            # habia una rotacion abierta, ese sigue siendo el de antes de empezar: el
            # token intermedio pudo perderse y guardarlo aqui dejaria al participante
            # sin ninguno valido.
            viejo = (p.get("token_anterior_sha256")
                     or p.get("token_sha256")
                     or (_sha(p["token"]) if p.get("token") else None))
            if not viejo:
                return JSONResponse({"error": "el participante no tiene token vigente"}, status_code=409)
            reintento = bool(p.get("token_anterior_sha256"))
            p.pop("token", None)
            p["token_anterior_sha256"] = viejo
            p["token_sha256"] = h_nuevo
            # Identificador de ESTA rotacion. Sin el, una confirmacion de una rotacion
            # ANTERIOR contaba como valida para la siguiente y rotacion_cerrar retiraba
            # el token de alguien que nunca confirmo el nuevo -- dejandolo fuera del
            # canal, que es exactamente lo que todo este mecanismo existe para evitar.
            # Encontrado el 2-sep-2026 probandolo dos veces seguidas sobre el mismo id.
            p["rot_id"] = secrets.token_hex(8)
            p["rota_hasta"] = (datetime.date.today() +
                               datetime.timedelta(days=int(d.get("dias", 7)))).isoformat()
            _guardar_participantes(); _recargar_participantes()
            d["estado"] = "canjeada"; d["canjeada"] = now()
            con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                        (json.dumps(d, ensure_ascii=False), now(), r["id"]))
            return JSONResponse({"ok": True, "id": pid, "reintento": reintento,
                "aviso": ("tu token ANTIGUO sigue valiendo. Reinicia tu cliente con el nuevo y "
                          "llama a token_confirmar() para que la autoridad pueda cerrar la rotacion")})
    return JSONResponse({"error": "codigo no valido"}, status_code=403)

@mcp.tool()
def altas_pendientes() -> str:
    """(SOLO autoridad) Solicitudes de alta esperando aprobación."""
    me = ident()
    if not es_autoridad(me): return "ERROR: altas_pendientes es de la autoridad."
    out = []
    for i in _rows("invitacion", 200):
        if i.get("estado") == "solicitada":
            out.append({k: v for k, v in i.items() if k not in ("codigo_sha256", "token_sha256")})
    return _jd(out)

@mcp.tool()
def alta_aprobar(id: str, nota: str = "", frase: str = "") -> str:
    """(SOLO autoridad) Aprueba una solicitud de alta: activa la identidad con el
    token que el candidato propuso (aquí solo vive su hash)."""
    me = ident()
    if not es_autoridad(me): return "ERROR: alta_aprobar es de la autoridad."
    ok, err = _frase_ok(frase)
    if not ok: return err
    pid = id.strip().lower()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("estado") == "solicitada" and d.get("id") == pid:
                if pid in PARTICIPANTES and PARTICIPANTES[pid].get("activo", True):
                    return f"ERROR: '{pid}' ya existe y esta activo."
                PARTICIPANTES[pid] = {"token_sha256": d["token_sha256"], "tipo": d.get("tipo", "cowork"),
                                      "nombre": d.get("nombre", pid), "maquina": d.get("maquina", ""),
                                      "desde": datetime.date.today().isoformat(), "activo": True,
                                      "alta_via": "registro", "aprobada_por": me}
                _guardar_participantes()
                _recargar_participantes()
                d["estado"] = "aprobada"; d["aprobada_por"] = me; d["aprobada_fecha"] = now()
                if nota: d["nota_aprobacion"] = nota
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return _jd({"accion": "aprobada", "id": pid,
                            "aviso": "el candidato ya puede usar el token que propuso"})
    return f"ERROR: no hay solicitud pendiente para '{pid}'."

@mcp.tool()
def alta_rechazar(id: str, motivo: str = "") -> str:
    """(SOLO autoridad) Rechaza una solicitud de alta pendiente."""
    me = ident()
    if not es_autoridad(me): return "ERROR: alta_rechazar es de la autoridad."
    pid = id.strip().lower()
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("estado") == "solicitada" and d.get("id") == pid:
                d["estado"] = "rechazada"; d["rechazada_por"] = me
                if motivo: d["motivo_rechazo"] = motivo
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return _jd({"accion": "rechazada", "id": pid})
    return f"ERROR: no hay solicitud pendiente para '{pid}'."

async def registro_post(request):
    """Alta remota sin token: requiere codigo de invitacion vigente de la autoridad."""
    try:
        body = json.loads(await request.body())
    except Exception:
        return JSONResponse({"error": "JSON invalido"}, status_code=400)
    codigo = str(body.get("codigo", "")); pid = str(body.get("id", "")).strip().lower()
    tok = str(body.get("token_propuesto", "")); tipo = str(body.get("tipo", "cowork")).strip().lower()
    if tipo not in ("cowork", "agente", "servicio", "humano"):
        return JSONResponse({"error": "tipo: cowork|agente|servicio|humano"}, status_code=400)
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,19}", pid):
        return JSONResponse({"error": "id invalido: minusculas, [a-z][a-z0-9-]{1,19}"}, status_code=400)
    if pid in PARTICIPANTES:
        return JSONResponse({"error": "id no disponible"}, status_code=409)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", tok):
        return JSONResponse({"error": "token_propuesto: 32-128 caracteres url-safe, generado por ti"}, status_code=400)
    h = _sha(codigo)
    with db() as con:
        for r in con.execute("SELECT id,data FROM items WHERE kind='invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("estado") == "solicitada" and d.get("id") == pid:
                return JSONResponse({"error": "id no disponible"}, status_code=409)
        for r in con.execute("SELECT id,data FROM items WHERE kind='invitacion'").fetchall():
            d = json.loads(r["data"])
            if d.get("codigo_sha256") == h and d.get("estado") == "emitida":
                try:
                    ed = datetime.datetime.fromisoformat(d.get("emitida"))
                    caducada = (datetime.datetime.now(datetime.timezone.utc) - ed).days >= 7
                except Exception:
                    caducada = False
                if caducada:
                    d["estado"] = "caducada"
                    con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                                (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                    return JSONResponse({"error": "invitacion caducada"}, status_code=410)
                d.update({"estado": "solicitada", "id": pid, "tipo": tipo,
                          "nombre": str(body.get("nombre", pid))[:80],
                          "maquina": str(body.get("maquina", ""))[:40],
                          "token_sha256": _sha(tok), "solicitada": now()})
                con.execute("UPDATE items SET data=?, updated=? WHERE id=?",
                            (json.dumps(d, ensure_ascii=False), now(), r["id"]))
                return JSONResponse({"ok": True, "id": pid,
                                     "estado": "pendiente de aprobacion por la autoridad"})
    return JSONResponse({"error": "invitacion no valida"}, status_code=404)

# ───────────── PUERTOS LOCALES (por estacion de trabajo) ─────────────
@mcp.tool()
def puerto_reservar(puerto: int, servicio: str, notas: str = "", hasta: int = 0) -> str:
    """Registra un puerto (o rango puerto..hasta) que ocupas EN TU ESTACION, para
    que nadie lo pise ni mate tu proceso creyendo que es huerfano. La estacion la
    pone el servidor: no puedes registrar puertos de otra maquina."""
    me = ident(); maq = estacion()
    if not maq:
        return "ERROR: tu identidad no tiene estacion asignada; pidele a la autoridad que la registre."
    try:
        p1 = int(puerto); p2 = int(hasta) if hasta else p1
    except (TypeError, ValueError):
        return "ERROR: puerto y hasta deben ser numeros."
    if not (1 <= p1 <= 65535 and p1 <= p2 <= 65535):
        return "ERROR: rango invalido (1-65535, y hasta >= puerto)."
    if not servicio.strip():
        return "ERROR: di que servicio ocupa el puerto (es el dato que evita que alguien lo mate)."
    for r in _rows("puerto", 500, order="DESC"):
        if r.get("maquina") != maq or r.get("estado") != "ocupado": continue
        if _solapa(p1, p2, int(r["puerto"]), int(r.get("hasta") or r["puerto"])):
            if r.get("dueno") != me:
                return (f"ERROR: en {maq} el puerto {r['puerto']}"
                        + (f"-{r['hasta']}" if r.get("hasta") and r["hasta"] != r["puerto"] else "")
                        + f" ya es de '{r.get('dueno')}' ({r.get('servicio')}). Elige otro o hablalo con quien lo tiene.")
    d = {"maquina": maq, "puerto": p1, "hasta": p2, "servicio": servicio.strip(),
         "dueno": me, "notas": notas, "estado": "ocupado"}
    res = _put("puerto", f"{maq}:{p1}", d)
    return _jd({**res, "maquina": maq, "puerto": p1,
                **({"hasta": p2} if p2 != p1 else {})})

@mcp.tool()
def puerto_list(maquina: str = "", incluir_libres: bool = False) -> str:
    """Puertos registrados EN TU ESTACION. Otra maquina solo se consulta pidiendola
    explicitamente, y los agentes nunca ven mas que la suya: lo de otra estacion es
    ruido para ti y contexto ajeno para ellos."""
    me = ident(); mia = estacion()
    pedida = (maquina or "").strip() or mia
    if pedida != mia:
        tipo = PARTICIPANTES.get(me, {}).get("tipo")
        if tipo == "agente" and not es_autoridad(me):
            return "ERROR: un agente solo consulta los puertos de su propia estacion."
    out = [r for r in _rows("puerto", 500, order="DESC")
           if r.get("maquina") == pedida and (incluir_libres or r.get("estado") == "ocupado")]
    out.sort(key=lambda r: int(r["puerto"]))
    return _jd({"estacion": pedida, "total": len(out), "puertos": out})

@mcp.tool()
def puerto_quien(puerto: int) -> str:
    """De quien es un puerto EN TU ESTACION. Pregúntalo ANTES de matar un proceso
    que no reconozcas: puede ser el cerebro o una corrida de otro cowork."""
    maq = estacion()
    if not maq: return "ERROR: tu identidad no tiene estacion asignada."
    try: p = int(puerto)
    except (TypeError, ValueError): return "ERROR: puerto debe ser un numero."
    for r in _rows("puerto", 500, order="DESC"):
        if r.get("maquina") == maq and r.get("estado") == "ocupado" \
                and _solapa(p, p, int(r["puerto"]), int(r.get("hasta") or r["puerto"])):
            return _jd({"encontrado": True, "estacion": maq, "puerto": p,
                        "dueno": r.get("dueno"), "servicio": r.get("servicio"),
                        "rango": f"{r['puerto']}-{r['hasta']}" if r.get("hasta") and r["hasta"] != r["puerto"] else str(r["puerto"]),
                        "notas": r.get("notas", ""), "desde": r.get("_creado")})
    return _jd({"encontrado": False, "estacion": maq, "puerto": p,
                "aviso": "sin registrar: si hay algo escuchando ahi, pregunta en el canal antes de matarlo"})

@mcp.tool()
def puerto_liberar(puerto: int) -> str:
    """Libera un puerto tuyo (o cualquiera de tu estacion, si eres autoridad)."""
    me = ident(); maq = estacion()
    if not maq: return "ERROR: tu identidad no tiene estacion asignada."
    try: p = int(puerto)
    except (TypeError, ValueError): return "ERROR: puerto debe ser un numero."
    _, d = _get("puerto", f"{maq}:{p}")
    if not d or d.get("estado") != "ocupado":
        return f"ERROR: {p} no esta registrado como ocupado en {maq}."
    if d.get("dueno") != me and not es_autoridad(me):
        return f"ERROR: {p} en {maq} es de '{d.get('dueno')}'."
    d["estado"] = "libre"; d["liberado_por"] = me; d["liberado"] = now()
    return _jd({**_put("puerto", f"{maq}:{p}", d), "accion": "liberado", "puerto": p})

# ───────────── DECISIONES / HECHOS / INFRA (sellados con identidad) ─────────────
@mcp.tool()
def decision_log(titulo: str, decision: str, motivo: str, proyecto: str = "", supersede: int = 0) -> str:
    """Registra una decisión duradera (append-only; para cambiarla, nueva con `supersede`)."""
    if supersede:
        # Antes se aceptaba cualquier numero: una decision podia declarar que
        # supera a otra que no existe, y nadie se enteraba. Una referencia rota
        # que se guarda callada es peor que un rechazo.
        _i, _d = None, None
        with db() as con:
            _r = con.execute("SELECT data FROM items WHERE id=? AND kind='decision'",
                             (int(supersede),)).fetchone()
        if not _r:
            return (f"ERROR: no existe la decision {supersede}, asi que no se puede superar. "
                    "Mira decision_list() para el numero correcto.")
    d = {"de": ident(), "titulo": titulo, "decision": decision, "motivo": motivo, "proyecto": proyecto}
    if supersede: d["supersede"] = supersede
    return _jd(_append("decision", d))

@mcp.tool()
def decision_list(proyecto: str = "", limite: int = 25) -> str:
    """Decisiones, de la más reciente a la más antigua."""
    rows = _rows("decision", 300, order="DESC")
    if proyecto: rows = [r for r in rows if r.get("proyecto", "").lower() == proyecto.lower()]
    superseded = {r["supersede"] for r in rows if r.get("supersede")}
    for r in rows:
        if r["_id"] in superseded: r["_estado"] = "superada"
    return _jd(rows[:limite])

@mcp.tool()
def fact_set(clave: str, valor: str, fuente: str = "") -> str:
    """Fija un hecho canónico (dato que todos deben citar igual). Sin secretos."""
    return _jd(_put("fact", clave.strip().lower(),
                    {"clave": clave, "valor": valor, "fuente": fuente, "de": ident()}))

@mcp.tool()
def fact_get(clave: str) -> str:
    """Consulta un hecho canónico."""
    _, d = _get("fact", clave.strip().lower())
    return _jd(d) if d else f"No existe el hecho '{clave}'. Usa fact_list()."

@mcp.tool()
def fact_list(prefijo: str = "") -> str:
    """Lista los hechos canónicos."""
    rows = _rows("fact")
    if prefijo: rows = [r for r in rows if r.get("clave", "").lower().startswith(prefijo.lower())]
    return _jd(rows)

@mcp.tool()
def infra_put(id: str, tipo: str, descripcion: str, detalles: str = "{}") -> str:
    """Registra/actualiza un recurso de infraestructura. `detalles` JSON. Solo punteros, no secretos."""
    try:
        extra = json.loads(detalles) if detalles else {}
    except json.JSONDecodeError as e:
        return f"ERROR: detalles no es JSON: {e}"
    return _jd(_put("infra", id, {"id": id, "tipo": tipo, "descripcion": descripcion,
                                  "registrado_por": ident(), **extra}))

@mcp.tool()
def infra_list() -> str:
    """Servidores y servicios registrados."""
    return _jd(_rows("infra"))

@mcp.tool()
def search(texto: str, limite: int = 30) -> str:
    """Busca texto libre en todo el estado compartido."""
    if not texto.strip():
        return ("ERROR: search sin texto devolveria el canal entero, que casi nunca es lo "
                "que se quiere y oculta que la busqueda estaba vacia. Di que buscas.")
    q = f"%{texto.lower()}%"
    with db() as con:
        cur = con.execute("SELECT id,kind,key,data,updated FROM items WHERE lower(data) LIKE ? "
                          "ORDER BY id DESC LIMIT ?", (q, limite))
        out = [{"_id": r["id"], "_tipo": r["kind"], "_clave": r["key"],
                "_actualizado": r["updated"], **json.loads(r["data"])} for r in cur.fetchall()]
    return _jd(out)

# ───────────── SUBDOMINIOS Y DESPLIEGUE ─────────────
@mcp.tool()
def subdomain_claim(nombre: str, notas: str = "") -> str:
    """Reserva un subdominio para MI identidad. El despliegue es por HTTPS con tu
    token (ver deploy_info()), no hace falta SSH."""
    me = ident()
    nombre = nombre.strip().lower()
    if not NOMBRE_RE.match(nombre): return "ERROR: solo minúsculas, números y guiones (max 31)."
    if nombre in RESERVADOS: return f"ERROR: '{nombre}' está reservado."
    _, d = _get("subdomain", nombre)
    if d and d.get("estado") in ("ocupado", "solicitado") and d.get("dueno") != me:
        return f"ERROR: '{nombre}' ya es de '{d.get('dueno')}' (estado {d.get('estado')})."
    if es_autoridad(me):
        res = _put("subdomain", nombre, {"nombre": nombre, "dueno": me, "estado": "ocupado",
                                         "url": f"https://{nombre}.{DOMAIN}", "notas": notas,
                                         "aprobado_por": me, "aprobado": now()})
        return _jd({**res, "estado": "ocupado", "url": f"https://{nombre}.{DOMAIN}",
                    "siguiente_paso": "PUT del tar.gz a /<TU_TOKEN>/deploy/" + nombre + " (estatico) o /<TU_TOKEN>/app/" + nombre + " (dinamico)"})
    res = _put("subdomain", nombre, {"nombre": nombre, "dueno": me, "estado": "solicitado",
                                     "url": f"https://{nombre}.{DOMAIN}", "notas": notas,
                                     "solicitado": now()})
    return _jd({**res, "estado": "solicitado",
                "siguiente_paso": "pendiente de aprobacion de la autoridad; hasta entonces no se puede desplegar ni se emite certificado TLS"})

@mcp.tool()
def subdomain_list(solo_ocupados: bool = False) -> str:
    """Subdominios registrados."""
    rows = _rows("subdomain")
    if solo_ocupados: rows = [r for r in rows if r.get("estado") == "ocupado"]
    return _jd(rows)

@mcp.tool()
def subdomain_release(nombre: str) -> str:
    """Libera un subdominio propio (no borra archivos ni apps)."""
    me = ident()
    nombre = nombre.strip().lower()
    _, d = _get("subdomain", nombre)
    if not d: return f"ERROR: '{nombre}' no está registrado."
    if d.get("dueno") != me and not es_autoridad(me):
        return f"ERROR: '{nombre}' es de '{d.get('dueno')}'; solo su dueno o la autoridad puede liberarlo."
    d["estado"] = "libre"; d["liberado_por"] = me; d["liberado"] = now()
    return _jd(_put("subdomain", nombre, d))

@mcp.tool()
def subdomain_pendientes() -> str:
    """(SOLO autoridad) Subdominios solicitados esperando aprobación."""
    if not es_autoridad(ident()): return "ERROR: subdomain_pendientes es de la autoridad."
    return _jd([r for r in _rows("subdomain", 300) if r.get("estado") == "solicitado"])

@mcp.tool()
def subdomain_aprobar(nombre: str, nota: str = "") -> str:
    """(SOLO autoridad) Aprueba un subdominio solicitado: habilita despliegue y TLS."""
    me = ident()
    if not es_autoridad(me): return "ERROR: subdomain_aprobar es de la autoridad."
    nombre = nombre.strip().lower()
    _, d = _get("subdomain", nombre)
    if not d: return f"ERROR: '{nombre}' no está registrado."
    if d.get("estado") != "solicitado": return f"ERROR: '{nombre}' está en estado {d.get('estado')}, no solicitado."
    d["estado"] = "ocupado"; d["aprobado_por"] = me; d["aprobado"] = now()
    if nota: d["nota_aprobacion"] = nota
    _put("subdomain", nombre, d)
    return _jd({"accion": "aprobado", "nombre": nombre, "dueno": d.get("dueno"),
                "url": f"https://{nombre}.{DOMAIN}"})

@mcp.tool()
def subdomain_rechazar(nombre: str, motivo: str = "") -> str:
    """(SOLO autoridad) Rechaza un subdominio solicitado."""
    me = ident()
    if not es_autoridad(me): return "ERROR: subdomain_rechazar es de la autoridad."
    nombre = nombre.strip().lower()
    _, d = _get("subdomain", nombre)
    if not d: return f"ERROR: '{nombre}' no está registrado."
    if d.get("estado") != "solicitado": return f"ERROR: '{nombre}' está en estado {d.get('estado')}, no solicitado."
    d["estado"] = "rechazado"; d["rechazado_por"] = me; d["rechazado"] = now()
    if motivo: d["motivo_rechazo"] = motivo
    _put("subdomain", nombre, d)
    return _jd({"accion": "rechazado", "nombre": nombre})

@mcp.tool()
def app_dormir(nombre: str) -> str:
    """Duerme una app: la para sin desinstalarla. Vuelve sola al primer visitante
    (scale-to-zero). Solo el dueño o la autoridad."""
    me = ident()
    nombre = nombre.strip().lower()
    _, d = _get("app", nombre)
    if not d or d.get("estado") == "eliminada": return f"ERROR: no existe la app '{nombre}'."
    if d.get("dueno") != me and not es_autoridad(me):
        return f"ERROR: '{nombre}' es de '{d.get('dueno')}'."
    rc, out = _sudo_ctl("stop", nombre)
    d["ultimo_dormir"] = now(); _put("app", nombre, d)
    return _jd({"accion": "dormida", "nombre": nombre,
                "aviso": "despierta sola con la primera visita a su subdominio"})

@mcp.tool()
def app_eliminar(nombre: str) -> str:
    """Elimina una app dinámica: para el servicio, borra su unidad y su snippet de
    Caddy. Solo el dueño o la autoridad. Los archivos desplegados se conservan."""
    me = ident()
    nombre = nombre.strip().lower()
    _, d = _get("app", nombre)
    if not d: return f"ERROR: no existe la app '{nombre}'."
    if d.get("dueno") != me and not es_autoridad(me):
        return f"ERROR: '{nombre}' es de '{d.get('dueno')}'; solo su dueno o la autoridad puede eliminarla."
    rc, out = _sudo_ctl("remove", nombre)
    if rc != 0: return f"ERROR al eliminar: {out[:300]}"
    d["estado"] = "eliminada"; d["eliminada_por"] = me; d["eliminada"] = now()
    _put("app", nombre, d)
    return _jd({"accion": "eliminada", "nombre": nombre, "detalle": out[:200]})

@mcp.tool()
def deploy_info() -> str:
    """Cómo desplegar un sitio estático o una app dinámica en <nombre>.<dominio>."""
    return _jd({
        "estatico": {
            "1": "subdomain_claim('<nombre>')",
            "2": "empaquetar: tar -czf sitio.tar.gz -C ./build .",
            "3": f"PUT https://{PUBLIC_HOST}/<TU_TOKEN>/deploy/<nombre>  (cuerpo = tar.gz)",
            "4": f"listo en https://<nombre>.{DOMAIN}",
        },
        "app_dinamica": {
            "1": "subdomain_claim('<nombre>')",
            "2": "incluir eva-app.toml en la raíz con:  cmd = \"python3 servidor.py\"  (escucha en $PORT)",
            "3": f"PUT https://{PUBLIC_HOST}/<TU_TOKEN>/app/<nombre>  (cuerpo = tar.gz)",
            "4": f"el servidor la corre en sandbox systemd y la publica en https://<nombre>.{DOMAIN}",
            "limites": "512M RAM, 80% CPU, sin privilegios, reinicio automático",
            "gestion": "app_status / app_logs / app_restart / app_stop / app_start",
        },
        "maximo": "50 MB por despliegue",
        "nota": "el tar.gz no debe contener rutas absolutas ni '..'",
    })

SPOOL = os.environ.get("EVASTATE_SPOOL", "/var/lib/evastate/ctl-spool")
CTL_OUT = os.environ.get("EVASTATE_CTL_OUT", "/var/lib/evastate/ctl-out")
os.makedirs(SPOOL, exist_ok=True); os.makedirs(CTL_OUT, exist_ok=True)

def _ctl_pedir(accion, nombre, extra=None):
    """Deja la petición en el spool; el ayudante root (eva-appd) la ejecuta.
    Así el servidor conserva su sandbox (ProtectSystem=strict) intacto."""
    import uuid, time
    rid = uuid.uuid4().hex
    req = {"id": rid, "accion": accion, "nombre": nombre, **(extra or {})}
    tmp = os.path.join(SPOOL, f".{rid}.tmp")
    with open(tmp, "w") as f: json.dump(req, f)
    pet_p = os.path.join(SPOOL, f"{rid}.json")
    os.replace(tmp, pet_p)
    res_p = os.path.join(CTL_OUT, f"{rid}.json")
    for i in range(240):                      # hasta 120 s
        if os.path.exists(res_p):
            res = json.load(open(res_p)); os.remove(res_p)
            return res.get("rc", 1), res.get("out", "")
        # El ayudante borra la peticion al recogerla. Si sigue ahi pasado el
        # margen de arranque, nadie la va a recoger: no hacemos esperar 2 min.
        if i == 16 and os.path.exists(pet_p):
            os.remove(pet_p)
            return 1, ("ERROR: las apps dinamicas necesitan el ayudante eva-appd "
                       "(systemd) y aqui no esta activo. El resto del canal "
                       "funciona con normalidad.")
        time.sleep(0.5)
    return 1, "timeout: el ayudante eva-appd no respondio en 120 s"

def _sudo_ctl(*args):
    accion, nombre = args[0], args[1]
    extra = {}
    if accion == "install": extra["puerto"] = int(args[2])
    if accion == "logs" and len(args) > 2: extra["lineas"] = int(args[2])
    return _ctl_pedir(accion, nombre, extra)

def _app_owned(nombre):
    me = ident()
    nombre = nombre.strip().lower()
    if not NOMBRE_RE.match(nombre): return None, "ERROR: nombre inválido."
    _, d = _get("subdomain", nombre)
    if not d or d.get("dueno") != me:
        return None, f"ERROR: '{nombre}' no es un subdominio tuyo (subdomain_claim primero)."
    return nombre, None

@mcp.tool()
def app_list() -> str:
    """Apps dinámicas registradas y su estado."""
    rows = _rows("app")
    for r in rows:
        rc, out = _sudo_ctl("status", r["_key"])
        r["_estado_systemd"] = out.splitlines()[0] if out else "?"
    return _jd(rows)

@mcp.tool()
def app_status(nombre: str) -> str:
    """Estado systemd de una app propia."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("status", nombre)
    return out or "sin salida"

@mcp.tool()
def app_logs(nombre: str, lineas: int = 40) -> str:
    """Últimas líneas de log de una app propia."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("logs", nombre, str(min(int(lineas), 200)))
    return out or "sin salida"

@mcp.tool()
def app_restart(nombre: str) -> str:
    """Reinicia una app propia."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("restart", nombre)
    return out or ("ok" if rc == 0 else f"ERROR rc={rc}")

@mcp.tool()
def app_stop(nombre: str) -> str:
    """Detiene una app propia (el subdominio vuelve a servir lo estático si existe)."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("stop", nombre)
    return out or ("ok" if rc == 0 else f"ERROR rc={rc}")

@mcp.tool()
def app_start(nombre: str) -> str:
    """Arranca una app propia detenida."""
    nombre, err = _app_owned(nombre)
    if err: return err
    rc, out = _sudo_ctl("start", nombre)
    return out or ("ok" if rc == 0 else f"ERROR rc={rc}")

# ───────────── HTTP: salud, tls-check, despliegues ─────────────
async def health(_):
    with db() as con:
        n = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    return PlainTextResponse(f"ok · {n} registros")

async def tls_check(request):
    host = (request.query_params.get("domain") or "").strip().lower()
    if host in (DOMAIN, f"www.{DOMAIN}", PUBLIC_HOST):
        return PlainTextResponse("ok")
    if not host.endswith(f".{DOMAIN}"): return PlainTextResponse("no", status_code=404)
    nombre = host[: -len(f".{DOMAIN}")]
    if "." in nombre: return PlainTextResponse("no", status_code=404)
    _, d = _get("subdomain", nombre)
    if d and d.get("estado") == "ocupado": return PlainTextResponse("ok")
    return PlainTextResponse("no", status_code=404)

def _extraer_seguro(blob, destino):
    """Extrae tar.gz rechazando rutas absolutas, '..' y enlaces."""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf.getmembers():
            p = m.name
            if p.startswith("/") or ".." in p.split("/") or m.issym() or m.islnk():
                raise ValueError(f"entrada insegura en el tar: {p}")
        os.makedirs(destino, exist_ok=True)
        _vaciar(destino)
        tf.extractall(destino)

def _vaciar(d):
    """Deja el directorio existente pero sin contenido. Sin binarios externos:
    `find -delete` no existe en Windows."""
    import shutil
    for e in os.scandir(d):
        if e.is_dir() and not e.is_symlink():
            shutil.rmtree(e.path, ignore_errors=True)
        else:
            try: os.unlink(e.path)
            except OSError: pass

async def _leer_cuerpo(request):
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD:
            return None
    return body

async def deploy_static(request, pid):
    nombre = request.path_params["nombre"].strip().lower()
    if not NOMBRE_RE.match(nombre):
        return JSONResponse({"error": "nombre inválido"}, status_code=400)
    _, d = _get("subdomain", nombre)
    if not d or d.get("dueno") != pid or d.get("estado") != "ocupado":
        est = (d or {}).get("estado")
        if d and d.get("dueno") == pid and est == "solicitado":
            return JSONResponse({"error": f"'{nombre}' espera aprobacion de la autoridad (subdomain_aprobar)"}, status_code=403)
        return JSONResponse({"error": f"'{nombre}' no esta aprobado para '{pid}' (subdomain_claim y aprobacion de la autoridad)"}, status_code=403)
    body = await _leer_cuerpo(request)
    if body is None: return JSONResponse({"error": "más de 50 MB"}, status_code=413)
    try:
        _extraer_seguro(body, os.path.join(SITES_DIR, nombre))
    except Exception as e:
        return JSONResponse({"error": f"tar inválido: {e}"}, status_code=400)
    d["ultimo_deploy"] = {"por": pid, "fecha": now(), "bytes": len(body), "tipo": "estatico"}
    _put("subdomain", nombre, d)
    return JSONResponse({"ok": True, "url": f"https://{nombre}.{DOMAIN}", "bytes": len(body)})

async def deploy_app(request, pid):
    nombre = request.path_params["nombre"].strip().lower()
    if not NOMBRE_RE.match(nombre):
        return JSONResponse({"error": "nombre inválido"}, status_code=400)
    _, d = _get("subdomain", nombre)
    if not d or d.get("dueno") != pid or d.get("estado") != "ocupado":
        est = (d or {}).get("estado")
        if d and d.get("dueno") == pid and est == "solicitado":
            return JSONResponse({"error": f"'{nombre}' espera aprobacion de la autoridad (subdomain_aprobar)"}, status_code=403)
        return JSONResponse({"error": f"'{nombre}' no esta aprobado para '{pid}' (subdomain_claim y aprobacion de la autoridad)"}, status_code=403)
    body = await _leer_cuerpo(request)
    if body is None: return JSONResponse({"error": "más de 50 MB"}, status_code=413)
    destino = os.path.join(APPS_DIR, nombre)
    try:
        _extraer_seguro(body, destino)
    except Exception as e:
        return JSONResponse({"error": f"tar inválido: {e}"}, status_code=400)
    if not os.path.exists(os.path.join(destino, "eva-app.toml")):
        return JSONResponse({"error": "falta eva-app.toml (con cmd = \"...\")"}, status_code=400)
    # puerto estable por app
    aid, ad = _get("app", nombre)
    if ad and ad.get("puerto"):
        puerto = ad["puerto"]
    else:
        usados = {a.get("puerto") for a in _rows("app")}
        puerto = next(p for p in range(9100, 9900) if p not in usados)
    rc, out = _sudo_ctl("install", nombre, str(puerto))
    if rc != 0:
        return JSONResponse({"error": f"instalación falló: {out}"}, status_code=500)
    _put("app", nombre, {"nombre": nombre, "dueno": pid, "puerto": puerto,
                         "ultimo_deploy": {"por": pid, "fecha": now(), "bytes": len(body)}})
    return JSONResponse({"ok": True, "url": f"https://{nombre}.{DOMAIN}",
                         "puerto_interno": puerto, "detalle": out})

BACKUP_DIR = os.environ.get("EVASTATE_BACKUP_DIR", "/var/backups/evastate")

async def backup_get(request, pid):
    """Devuelve el respaldo mas reciente (gz). Cada cowork baja su copia al
    abrir sesion: cuatro copias en cuatro sitios."""
    import glob as _g
    files = sorted(_g.glob(os.path.join(BACKUP_DIR, "state-*.db.gz")))
    if not files:
        return JSONResponse({"error": "aun no hay respaldo"}, status_code=404)
    f = files[-1]
    data = open(f, "rb").read()
    from starlette.responses import Response
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="{os.path.basename(f)}"',
        "X-Backup-Sha256": __import__("hashlib").sha256(data).hexdigest(),
    })

# ───────────── ENSAMBLE ─────────────
@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield

PAGINA_WAKE = """<!doctype html><meta charset="utf-8"><title>Demo en reposo</title>
<meta name="robots" content="noindex">
<style>body{font-family:system-ui,sans-serif;background:#0f0f0f;color:#e6e6e6;display:flex;
height:100vh;margin:0;align-items:center;justify-content:center;text-align:center}
.c{max-width:430px}h1{font-size:19px;font-weight:600;margin:0 0 8px}
p{font-size:14px;color:#9a9a9a;line-height:1.6}b{color:#d50c2d}
button{font:inherit;font-weight:600;margin-top:14px;padding:9px 20px;border-radius:6px;
border:1px solid #d50c2d;background:#d50c2d;color:#fff;cursor:pointer}
#e{font-size:13px;color:#9a9a9a;margin-top:12px}</style>
<div class="c"><h1><b>%(n)s</b> está en reposo</h1>
<p>Esta demo se apaga cuando nadie la usa, para no consumir recursos.
Pulsa para encenderla: tarda unos segundos.</p>
<button id="b" onclick="ir()">Encender demo</button><div id="e"></div>
<script>
async function ir(){
  document.getElementById("b").disabled=true;
  document.getElementById("e").textContent="Encendiendo…";
  try{ await fetch("/wake/%(n)s",{method:"POST"}); }catch(x){}
  setTimeout(()=>location.reload(), 4000);
}
</script></div>"""

async def wake_get(nombre, arrancar=False):
    """Puerta del scale-to-zero. GET solo muestra la pagina de encendido: los
    escaneres de internet barren los subdominios sin parar y arrancarian las
    demos solas. Solo el POST del boton (accion humana) enciende la app."""
    nombre = (nombre or "").strip().lower()
    if not NOMBRE_RE.match(nombre):
        return PlainTextResponse("no", status_code=404)
    _, d = _get("app", nombre)
    if not d or d.get("estado") == "eliminada":
        return PlainTextResponse("no", status_code=404)
    if arrancar:
        rc, out = _sudo_ctl("status", nombre)
        if "ActiveState=active" not in out:
            _sudo_ctl("start", nombre)
            d["ultimo_despertar"] = now(); d["despertares"] = d.get("despertares", 0) + 1
            _put("app", nombre, d)
        return JSONResponse({"ok": True, "nombre": nombre}, headers={"Cache-Control": "no-store"})
    return HTMLResponse(PAGINA_WAKE % {"n": nombre}, status_code=503,
                        headers={"Cache-Control": "no-store", "Retry-After": "10"})

async def panel_get(_):
    """Panel de administracion: pagina estatica same-origin; la identidad la pone
    el token que el usuario introduce (no hay sesion en el servidor)."""
    try:
        return HTMLResponse(open(PANEL_PATH, encoding="utf-8").read(), headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store"})
    except Exception:
        return PlainTextResponse("panel no instalado", status_code=404)

async def sitio_get(request):
    """Sirve /s/<nombre>/... desde SITES_DIR cuando no hay proxy delante.
    Resuelve en cada peticion: un sitio recien desplegado aparece sin reiniciar."""
    nombre = request.path_params["nombre"]
    if not NOMBRE_RE.match(nombre):
        return PlainTextResponse("no encontrado", status_code=404)
    raiz = os.path.realpath(os.path.join(SITES_DIR, nombre))
    pedido = request.path_params.get("resto") or "index.html"
    destino = os.path.realpath(os.path.join(raiz, pedido))
    if destino != raiz and not destino.startswith(raiz + os.sep):
        return PlainTextResponse("no encontrado", status_code=404)
    if os.path.isdir(destino):
        destino = os.path.join(destino, "index.html")
    if not os.path.isfile(destino):
        return PlainTextResponse("no encontrado", status_code=404)
    from starlette.responses import FileResponse
    return FileResponse(destino)

_rutas = [Route("/health", health), Route("/tls-check", tls_check), Route("/panel", panel_get)]
if SERVE_SITES:
    _rutas += [Route("/s/{nombre}/{resto:path}", sitio_get), Route("/s/{nombre}", sitio_get)]

base = Starlette(routes=_rutas, lifespan=lifespan)
# El SDK rechaza con 421 cualquier Host que no este aqui (anti DNS-rebinding).
# Sin el puerto explicito, una instalacion que no escuche en 443 no responde.
_EXTRA_HOSTS = [h.strip() for h in os.environ.get("EVASTATE_EXTRA_HOSTS", "").split(",") if h.strip()]
_HOSTS = [PUBLIC_HOST, f"{PUBLIC_HOST}:443", "127.0.0.1:*", "localhost:*"] + _EXTRA_HOSTS

def _saludo(pid):
    """Lo que el canal tiene que decirle a ESTA identidad nada mas conectarse.
    Va en las instructions del handshake: el cliente lo ve antes de preguntar
    nada, sin depender de que se acuerde de llamar a state_overview."""
    if not pid or pid not in PARTICIPANTES:
        return None
    lineas = []
    msgs = _rows("msg", 500, order="DESC")
    privados = [m for m in msgs if m.get("para") == pid and m.get("de") != pid
                and m.get("estado") not in ("atendido", "respondida", "descartada")]
    avisos = [m for m in msgs if m.get("para") == "todos" and m.get("de") != pid
              and m.get("estado") not in ("atendido", "respondida", "descartada")]
    carteles = [c for c in _rows("cartel", 300, order="DESC")
                if c.get("estado") == "activo" and pid in c.get("dirigido_a", [])
                and (PARTICIPANTES.get(pid) or {}).get("confirma_cartelera", True)
                and pid not in c.get("confirmaciones", {})
                and c.get("requiere") in ("confirmacion", "respuesta")]
    if carteles:
        reglas = [c for c in carteles if c["tipo"] == "regla"]
        peticiones = [c for c in carteles if c["tipo"] != "regla"]
        if reglas:
            lineas.append("REGLAS SIN CONFIRMAR (cartel_confirmar): " +
                          ", ".join(f"{c['ref']} {c['asunto']}" for c in reglas[:5]))
        if peticiones:
            lineas.append("PETICIONES DE LA AUTORIDAD SIN RESPONDER (en privado a quien la emitio): " +
                          ", ".join(f"{c['ref']} {c['asunto']}" for c in peticiones[:5]))
    if privados:
        _entregar(pid, privados)   # nombrarselos ES entregarselos: queda la hora
        lineas.append(f"MENSAJES PRIVADOS PARA TI: {len(privados)} — " +
                      "; ".join(f"{m['de']}: {m['asunto'][:60]}" for m in privados[:4]))
    if avisos:
        lineas.append(f"AVISOS AL CANAL SIN ATENDER: {len(avisos)} — " +
                      "; ".join(f"{m['de']}: {m['asunto'][:50]}" for m in avisos[:3]))
    try:
        hoy_ = _hoy()
        fs = [r for r in _rows("fecha", 500, order="DESC")
              if r.get("dueno") == pid and r.get("estado") not in FINALES]
        vencidas = [r for r in fs if _dias(hoy_, r["cuando"]) < 0]
        pronto = [r for r in fs if 0 <= _dias(hoy_, r["cuando"]) <= 3]
        blq = [r for r in fs if r.get("estado") == "bloqueada"]
        if vencidas:
            lineas.insert(0, "FECHAS TUYAS YA VENCIDAS: " + ", ".join(
                f"{r['_key']} {r['que'][:40]} (era {r['cuando']})" for r in vencidas[:4]) +
                " — muevelas con fecha_mover o cierralas con fecha_estado")
        if pronto:
            lineas.append("VENCEN EN 3 DIAS O MENOS: " + ", ".join(
                f"{r['_key']} {r['que'][:40]} ({r['cuando']})" for r in pronto[:4]))
        if blq:
            lineas.append("TUYAS BLOQUEADAS: " + ", ".join(
                f"{r['_key']} {r['que'][:40]}" for r in blq[:3]))
    except Exception:
        pass
    abiertas = [m for m in msgs if m.get("de") == pid and m.get("tipo") == "solicitud"
                and m.get("estado") == "abierta"]
    if abiertas:
        det = []
        for m in abiertas[:6]:
            r_, q_ = m.get("ref", "?"), m.get("para")
            det.append(f"{r_} ({q_} LA LEYO y no ha respondido)" if m.get("visto")
                       else f"{r_} ({q_} aun no la ha abierto)")
        lineas.append("TUS SOLICITUDES ABIERTAS: " + ", ".join(det) +
                      " — si ya no necesitas nada del otro, cierralas TU con sol_cerrar")
    try:
        if CON_TOKEN_VIEJO.get():
            hasta = (PARTICIPANTES.get(pid) or {}).get("rota_hasta") or "(sin fecha)"
            lineas.insert(0,
                "ESTAS ENTRANDO CON TU TOKEN ANTIGUO. Hay uno nuevo esperandote y el viejo se "
                f"retira cuando TODOS hayan confirmado (previsto: {hasta}). Cambialo en tu "
                "configuracion y llama a token_confirmar() para que conste. Mientras no lo hagas, "
                "la rotacion no avanza y estas bloqueando al resto — nadie te va a cortar sin "
                "que hayas confirmado tu, pero nadie puede confirmarlo por ti.")
    except Exception:
        pass
    if not lineas:
        return f"Canal state. Identidad: {pid}. No tienes nada pendiente."
    return (f"Canal state. Identidad: {pid}. Atiende esto antes de trabajar:\n- " +
            "\n- ".join(lineas) +
            "\n\nDetalle con state_overview(). El servidor sella tu identidad: "
            "no existe ningun parametro para decir quien eres.")

def _instrucciones_dinamicas():
    """El SDK guarda instructions como atributo fijo al construir el servidor.
    Aqui se convierte en algo que se calcula por identidad en cada handshake."""
    try:
        low = mcp._lowlevel_server
        cls = type(low)
        base = getattr(low, "instructions", None)
        def _get(self):
            try:
                return _saludo(CURRENT.get(None)) or base
            except Exception:
                return base
        cls.instructions = property(_get, lambda self, v: None)
        return True
    except Exception as e:
        print(f"AVISO: instrucciones dinamicas no disponibles: {e}", file=sys.stderr)
        return False

SALUDO_DINAMICO = _instrucciones_dinamicas()

mcp_asgi = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_HOSTS,
        allowed_origins=[f"https://{PUBLIC_HOST}"] + [f"https://{h}" for h in _EXTRA_HOSTS],
    ),
)

async def app(scope, receive, send):
    if scope["type"] != "http":
        return await base(scope, receive, send)
    path = scope.get("path", "")
    partes = path.split("/")
    # /<token>/mcp | /<token>/deploy/<nombre> | /<token>/app/<nombre>
    if len(partes) >= 3 and partes[1] == "wake" and scope.get("method") in ("GET", "HEAD", "POST"):
        if not _rate_ok("__wake__", 60):
            return await PlainTextResponse("demasiadas peticiones", status_code=429)(scope, receive, send)
        resp = await wake_get(partes[2], arrancar=(scope["method"] == "POST"))
        return await resp(scope, receive, send)
    if len(partes) >= 2 and partes[1] == "rotacion" and scope.get("method") == "POST":
        if not _rate_ok("__rotacion__", 30):
            return await JSONResponse({"error": "demasiadas solicitudes"}, status_code=429)(scope, receive, send)
        from starlette.requests import Request
        resp = await rotacion_post(Request(scope, receive))
        return await resp(scope, receive, send)
    if len(partes) >= 2 and partes[1] == "registro" and scope.get("method") == "POST":
        if not _rate_ok("__registro__", 30):
            return await JSONResponse({"error": "demasiadas solicitudes"}, status_code=429)(scope, receive, send)
        from starlette.requests import Request
        resp = await registro_post(Request(scope, receive))
        return await resp(scope, receive, send)
    if len(partes) >= 3 and _sha(partes[1]) not in TOKEN_INDEX and partes[2] in ("mcp", "backup", "deploy", "app"):
        if not _rate_ok("__auth_fail__", 30):
            return await JSONResponse({"error": "demasiados intentos"}, status_code=429)(scope, receive, send)
    if len(partes) >= 3 and _sha(partes[1]) in TOKEN_INDEX:
        pid = TOKEN_INDEX[_sha(partes[1])]
        if not _rate_ok(pid):
            return await JSONResponse({"error": f"limite de tasa: {RATE_MAX} peticiones por {RATE_WINDOW}s"},
                                      status_code=429)(scope, receive, send)
        tokvar = CURRENT.set(pid)
        vjvar = CON_TOKEN_VIEJO.set(_sha(partes[1]) in TOKEN_VIEJOS)
        _tocar(pid)   # unico punto por el que pasa toda peticion: la huella no se puede olvidar
        try:
            resto = "/" + "/".join(partes[2:])
            if partes[2] == "mcp":
                sub = dict(scope); sub["path"] = resto; sub["raw_path"] = resto.encode()
                return await mcp_asgi(sub, receive, send)
            if partes[2] == "backup" and scope["method"] == "GET":
                resp = await backup_get(None, pid)
                return await resp(scope, receive, send)
            if partes[2] in ("deploy", "app") and len(partes) >= 4 and scope["method"] == "PUT":
                from starlette.requests import Request
                scope2 = dict(scope)
                scope2["path_params"] = {"nombre": partes[3]}
                req = Request(scope2, receive)
                req.scope["path_params"] = {"nombre": partes[3]}
                fn = deploy_static if partes[2] == "deploy" else deploy_app
                resp = await fn(req, pid)
                return await resp(scope, receive, send)
        finally:
            CURRENT.reset(tokvar)
        return await PlainTextResponse("ruta no reconocida bajo tu token", status_code=404)(scope, receive, send)
    return await base(scope, receive, send)

class _CensurarToken(logging.Filter):
    """El token viaja EN LA RUTA, asi que la linea de acceso de uvicorn escribe una
    credencial en claro en journald, y de ahi a syslog y a cualquier copia de logs.
    Detectado el 2-sep-2026 con 847 peticiones ya registradas asi, entre Caddy y el
    propio servidor. Aqui se sustituye el token por el nombre de su dueno ANTES de
    que se escriba: se conserva la utilidad de la linea y se pierde el secreto.
    Va como filtro dentro del log_config que se le pasa a uvicorn, no anadido al
    logger a mano: dictConfig reconstruye los loggers que declara y se llevaria por
    delante un filtro puesto antes."""
    _RE = re.compile(r"/([A-Za-z0-9_-]{20,})(?=/|$)")

    def filter(self, record):
        try:
            a = list(record.args or ())
            if len(a) >= 3 and isinstance(a[2], str) and "/" in a[2]:
                def _sub(m):
                    return "/[" + (TOKEN_INDEX.get(_sha(m.group(1))) or "token-invalido") + "]"
                a[2] = self._RE.sub(_sub, a[2])
                record.args = tuple(a)
        except Exception:
            pass   # censurar nunca puede tumbar el servicio; si falla, no se registra peor
        return True

if __name__ == "__main__":
    import uvicorn, copy
    extra = {}
    if TLS_CERT and TLS_KEY:
        extra = {"ssl_certfile": TLS_CERT, "ssl_keyfile": TLS_KEY}
    try:
        cfg = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
        cfg.setdefault("filters", {})["censura"] = {"()": _CensurarToken}
        cfg["loggers"]["uvicorn.access"]["filters"] = ["censura"]
        cfg["handlers"]["access"]["filters"] = ["censura"]
        extra["log_config"] = cfg
    except Exception as e:
        print(f"AVISO: no pude censurar el log de acceso ({e}); arranco SIN el.", file=sys.stderr)
    uvicorn.run(app, host=BIND, port=int(os.environ.get("EVASTATE_PORT", "8787")), **extra)
