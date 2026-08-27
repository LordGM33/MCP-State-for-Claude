#!/usr/bin/env python3
"""Shared-state MCP server: identity-sealed messaging, facts, decisions,
subdomain/app deployment. All config via EVASTATE_* env vars."""
import json, os, re, sqlite3, sys, datetime, contextlib, contextvars, io, tarfile, hashlib, secrets
import time
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
    with open(PARTICIPANTS_PATH) as f:
        data = json.load(f)
    idx = {}
    for pid, p in data.items():
        if not p.get("activo", True): continue
        h = p.get("token_sha256")
        if not h and p.get("token") and len(p["token"]) >= 24:
            h = _sha(p["token"])
        if h: idx[h] = pid
    return data, idx

PARTICIPANTES, TOKEN_INDEX = cargar_participantes()

def _recargar_participantes():
    global PARTICIPANTES, TOKEN_INDEX
    PARTICIPANTES, TOKEN_INDEX = cargar_participantes()
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
    with db() as con:
        cur = con.execute(
            f"SELECT id,key,data,created,updated FROM items WHERE kind=? ORDER BY id {order} LIMIT ?",
            (kind, limit))
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

def _next_ref(prefijo="SOL"):
    with db() as con:
        _, d = _get("seq", prefijo)
        n = (d or {}).get("n", 0) + 1
    _put("seq", prefijo, {"n": n})
    return f"{prefijo}-{n:03d}"

_REF_RE = re.compile(r"(?i)^(SOL|TEST|CART)-(\d+)$")
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

mcp = MCPServer(SERVER_NAME)

# ───────────── IDENTIDAD ─────────────
@mcp.tool()
def whoami() -> str:
    """Devuelve la identidad con la que este cliente escribe (la sella el servidor)."""
    pid = ident()
    p = {k: v for k, v in PARTICIPANTES[pid].items() if k not in ("token", "token_sha256")}
    return _jd({"id": pid, **p})

@mcp.tool()
def participantes() -> str:
    """Lista de participantes registrados (coworks, agentes, servicios, humanos)."""
    return _jd(_rows("participant"))

# ───────────── ARRANQUE ─────────────
@mcp.tool()
def state_overview() -> str:
    """Foto del estado compartido para MI identidad. Llamar al iniciar sesión."""
    me = ident()
    pend = [m for m in _rows("msg", 500, order="DESC")
            if m.get("para") in (me, "todos") and m.get("de") != me
            and m.get("estado") not in ("atendido", "respondida", "descartada")]
    mias = [m for m in _rows("msg", 500, order="DESC") if m.get("de") == me
            and m.get("tipo") == "solicitud" and m.get("estado") == "abierta"]
    cart_pend = []
    for c in _rows("cartel", 300, order="DESC"):
        if c.get("estado") != "activo": continue
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
    return _jd({
        "yo": me,
        "mi_estacion": maq or "(sin asignar)",
        "puertos_de_mi_estacion": puertos,
        **({"por_aprobar": por_aprobar} if por_aprobar else {}),
        "cartelera_pendiente": cart_pend,
        "esperando_respuesta": mias,
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
        # la solicitud del canal pasa a respondida (el hilo conserva todo);
        # las refs externas (archivo:...) no cierran nada: son enlace, no estado
        with db() as con:
            for r in con.execute("SELECT id,data FROM items WHERE kind='msg'").fetchall():
                dd = json.loads(r["data"])
                if _norm_ref(dd.get("ref")) == d["responde_a"] and dd.get("tipo") == "solicitud":
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
    return _jd(out)

@mcp.tool()
def msg_desde(fecha_iso: str) -> str:
    """Todo lo escrito desde una fecha (ISO: 2026-08-23 o 2026-08-23T15:00:00+00:00)."""
    rows = _rows("msg", 1000, order="ASC")
    return _jd([m for m in rows if m["_creado"] >= fecha_iso])

@mcp.tool()
def msg_hilo(ref: str) -> str:
    """El hilo completo de una ref (SOL-007): la solicitud y todas sus respuestas."""
    nref = _norm_ref(ref) or ref.strip()
    rows = _rows("msg", 1000, order="ASC")
    def _eq(v): return bool(v) and (_norm_ref(v) or v.strip()) == nref
    return _jd([m for m in rows if _eq(m.get("ref")) or _eq(m.get("responde_a"))])

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
    dirigidos = [p for p, v in PARTICIPANTES.items() if v.get("activo", True) and p != me]
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
            pend = [p for p in c.get("dirigido_a", []) if p not in conf]
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
def alta_aprobar(id: str, nota: str = "") -> str:
    """(SOLO autoridad) Aprueba una solicitud de alta: activa la identidad con el
    token que el candidato propuso (aquí solo vive su hash)."""
    me = ident()
    if not es_autoridad(me): return "ERROR: alta_aprobar es de la autoridad."
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

if __name__ == "__main__":
    import uvicorn
    extra = {}
    if TLS_CERT and TLS_KEY:
        extra = {"ssl_certfile": TLS_CERT, "ssl_keyfile": TLS_KEY}
    uvicorn.run(app, host=BIND, port=int(os.environ.get("EVASTATE_PORT", "8787")), **extra)
